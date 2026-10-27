"""Versioned, immutable connector contracts; fence requires one.

Revision ID: 0004_connector_contracts
Revises: 0003_durable_lifecycle

Adds safeact_connector_contracts (write-guarded) and
safeact_connector_contract(), and replaces safeact_begin_dispatch so the
fence refuses an operation without a registered contract (SAT10). No
contract rows are inserted and no roles or grants are created: the
production boundary stays deny-only.

The downgrade restores the 0003 fence and refuses while any contract
is registered.
"""

from alembic import op
import sqlalchemy as sa

from storage.lifecycle_sql.v0004 import (
    NEW_TABLES,
    downgrade_statements,
    upgrade_statements,
)


revision = "0004_connector_contracts"
down_revision = "0003_durable_lifecycle"
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

    for table in NEW_TABLES:
        op.execute(f"LOCK TABLE public.{table} IN ACCESS EXCLUSIVE MODE")
        rows = connection.execute(
            sa.text(f"SELECT count(*) FROM public.{table}")
        ).scalar_one()
        if rows != 0:
            raise RuntimeError(
                f"{table} holds registered contracts. Downgrade "
                "refused: decisions are bound to them."
            )

    _execute(downgrade_statements("public"))
