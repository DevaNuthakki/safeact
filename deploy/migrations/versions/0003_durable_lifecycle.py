"""Append-only durable lifecycle with protected transitions.

Revision ID: 0003_durable_lifecycle
Revises: 0002_canonical_payload

Existing 0001/0002 tables, constraints, and write guards are not
altered; one index is added to safeact_operations. No roles or
grants are created: every new function has EXECUTE revoked from
PUBLIC, so the production boundary stays deny-only.

Unlike 0001/0002, this revision has a working downgrade, but it
refuses to run while any execution history exists.
"""

from alembic import op
import sqlalchemy as sa

from storage.lifecycle_sql.v0003 import (
    HISTORY_TABLES,
    downgrade_statements,
    upgrade_statements,
)


revision = "0003_durable_lifecycle"
down_revision = "0002_canonical_payload"
branch_labels = None
depends_on = None


def _execute(statements):
    # Run on the migration's own DBAPI connection and transaction.
    # Composed SQL is sent without parameters, so plpgsql bodies
    # are never rewritten by bind-parameter processing.
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
        op.execute(
            f"LOCK TABLE public.{table} IN ACCESS EXCLUSIVE MODE"
        )

    for table in HISTORY_TABLES:
        rows = connection.execute(
            sa.text(f"SELECT count(*) FROM public.{table}")
        ).scalar_one()

        if rows != 0:
            raise RuntimeError(
                f"{table} holds execution history. Downgrade "
                "refused: it would destroy durable evidence."
            )

    _execute(downgrade_statements("public"))
