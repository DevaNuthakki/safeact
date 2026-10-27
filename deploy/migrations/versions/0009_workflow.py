"""The fixed two-step workflow (replacement_shipping_v1).

Revision ID: 0009_workflow
Revises: 0008_keys_escalation

Adds write-guarded safeact_workflows (the recorded plan) and
safeact_workflow_steps (one operation per step), a derived status
function and view, and the protected step-admission and scan
functions for the advancer role. No roles or grants.

The downgrade refuses while any workflow exists.
"""

from alembic import op
import sqlalchemy as sa

from storage.lifecycle_sql.v0009 import (
    HISTORY_TABLES,
    downgrade_statements,
    upgrade_statements,
)


revision = "0009_workflow"
down_revision = "0008_keys_escalation"
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

    for table in HISTORY_TABLES:
        op.execute(f"LOCK TABLE public.{table} IN ACCESS EXCLUSIVE MODE")
        rows = connection.execute(
            sa.text(f"SELECT count(*) FROM public.{table}")
        ).scalar_one()
        if rows != 0:
            raise RuntimeError(
                f"{table} holds workflow history. Downgrade refused."
            )

    _execute(downgrade_statements("public"))
