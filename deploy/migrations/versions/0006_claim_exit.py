"""Safe exit from CLAIMED after a crash.

Revision ID: 0006_claim_exit
Revises: 0005_deadlines_reopen

Seeds CLAIMED -> RETRY_ELIGIBLE (no attempt ever made) and
CLAIMED -> UNKNOWN (earlier attempts: re-verify). The append function
enforces which exit the attempt history allows; the recovery function
allows either only after the claim lease expired; the new
safeact_release_claim lets the claim owner leave at once. No roles or
grants.

The downgrade restores the 0005 functions and refuses while any
operation has left CLAIMED this way.
"""

from alembic import op
import sqlalchemy as sa

from storage.lifecycle_sql.v0006 import (
    LEGAL_TRANSITIONS,
    downgrade_statements,
    upgrade_statements,
)


revision = "0006_claim_exit"
down_revision = "0005_deadlines_reopen"
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
