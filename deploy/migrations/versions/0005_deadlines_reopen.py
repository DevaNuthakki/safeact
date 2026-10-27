"""Durable investigation deadlines and evidence-gated reopening.

Revision ID: 0005_deadlines_reopen
Revises: 0004_connector_contracts

Seeds ESCALATED_UNRESOLVED -> VERIFYING, adds the SQL investigation
deadline (latest dispatch + contract window) and a read-only status
function, and replaces the append and recovery functions so that:
after the deadline "verify later" is refused (SAT11); a reopen needs
decisive evidence recorded after the escalation (SAT09) and stays
within the contract's max_reopens (SAT14). No roles or grants.

The downgrade restores the 0003 functions and refuses while any
operation has been reopened.
"""

from alembic import op
import sqlalchemy as sa

from storage.lifecycle_sql.v0005 import (
    LEGAL_TRANSITIONS,
    downgrade_statements,
    upgrade_statements,
)


revision = "0005_deadlines_reopen"
down_revision = "0004_connector_contracts"
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

    for from_state, to_state in LEGAL_TRANSITIONS:
        used = connection.execute(sa.text(
            "SELECT count(*) FROM public.safeact_operation_transitions "
            "WHERE from_state = :a AND to_state = :b"
        ), {"a": from_state, "b": to_state}).scalar_one()
        if used:
            raise RuntimeError(
                f"{from_state} -> {to_state} appears in execution "
                "history. Downgrade refused."
            )

    _execute(downgrade_statements("public"))
