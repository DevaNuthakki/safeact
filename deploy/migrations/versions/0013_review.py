"""Version-bound human review of proposed next actions.

Revision ID: 0013_review
Revises: 0012_product_admission

Adds contract review policies, proposals, decisions and REVERIFY uses;
a provenance column on attempt outcomes (human provenance only for an
applied-effect reference); the propose, list, read and decide API
functions; the recovery-role retry-approval status, REVERIFY consumption
and success-by-provenance functions; and replaces
safeact_lifecycle_append with the 0012 rules plus the approval guard
on VERIFYING -> RETRY_ELIGIBLE. No roles or grants.

The downgrade refuses while any proposal or human-attested evidence
exists.
"""


from alembic import op
import sqlalchemy as sa

from storage.lifecycle_sql.v0013 import (
    downgrade_statements,
    upgrade_statements,
)


revision = "0013_review"
down_revision = "0012_product_admission"
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
        "LOCK TABLE public.safeact_proposals, public.safeact_attempt_outcomes "
        "IN ACCESS EXCLUSIVE MODE"
    )
    history = connection.execute(sa.text(
        "SELECT (SELECT count(*) FROM public.safeact_proposals)"
        " + (SELECT count(*) FROM public.safeact_attempt_outcomes"
        "    WHERE provenance <> 'system')"
    )).scalar_one()
    if history:
        raise RuntimeError(
            "Review history exists. Downgrade refused."
        )

    _execute(downgrade_statements("public"))
