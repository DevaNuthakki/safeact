"""Review follow-ups: expired retry approval, direct verification, policy binding.

Revision ID: 0015_review_followups
Revises: 0014_change_feed

Adds verification requests (an investigator's bounded, lookup-only
request) with their uses, the request and take functions, a report of
finished work by final reason, a guard in safeact_lifecycle_append for
the reason retry_approval_expired, and the review policy row in every
proposal binding (safeact_proposal_digest). No roles or grants.

The downgrade refuses while any verification request exists or any
operation ended with the reason retry_approval_expired.
"""


from alembic import op
import sqlalchemy as sa

from storage.lifecycle_sql.v0015 import (
    downgrade_statements,
    upgrade_statements,
)


revision = "0015_review_followups"
down_revision = "0014_change_feed"
branch_labels = None
depends_on = None


def _execute(statements):
    # Same raw-connection path as 0003: composed SQL, no parameters.
    raw = op.get_bind().connection.driver_connection

    for statement in statements:
        raw.execute(statement)


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    _execute(upgrade_statements("public"))


def downgrade():
    connection = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE public.safeact_verification_requests, "
        "public.safeact_operation_transitions IN ACCESS EXCLUSIVE MODE"
    )
    history = connection.execute(sa.text(
        "SELECT (SELECT count(*) FROM public.safeact_verification_requests)"
        " + (SELECT count(*) FROM public.safeact_operation_transitions"
        "    WHERE reason = 'retry_approval_expired')"
    )).scalar_one()
    if history:
        raise RuntimeError(
            "Review follow-up history exists. Downgrade refused."
        )

    _execute(downgrade_statements("public"))
