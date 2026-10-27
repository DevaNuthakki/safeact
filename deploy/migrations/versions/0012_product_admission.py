"""Authorized submission, workflow creation, reads and cancellation.

Revision ID: 0012_product_admission
Revises: 0011_identity_sessions

Adds the API-role functions that submit an action under an exact-scope
grant, create the fixed workflow for its requester, read operations
and workflows within the principal's tenant, list the principal's own
grants, and request cancellation. Adds the terminal state
CANCELLED_BEFORE_CLAIM with the pair RECORDED -> CANCELLED_BEFORE_CLAIM
and replaces safeact_lifecycle_append with the 0008 rules plus the
cancellation guard. No roles or grants.

The downgrade refuses while any operation was cancelled.
"""


from alembic import op
import sqlalchemy as sa

from storage.lifecycle_sql.v0012 import (
    downgrade_statements,
    upgrade_statements,
)


revision = "0012_product_admission"
down_revision = "0011_identity_sessions"
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
        "LOCK TABLE public.safeact_operation_transitions "
        "IN ACCESS EXCLUSIVE MODE"
    )
    cancelled = connection.execute(sa.text(
        "SELECT count(*) FROM public.safeact_operation_transitions "
        "WHERE to_state = 'CANCELLED_BEFORE_CLAIM'"
    )).scalar_one()
    if cancelled:
        raise RuntimeError(
            "Cancelled operations exist in history. Downgrade refused."
        )

    _execute(downgrade_statements("public"))
