"""Server-derived provider keys; escalate unreclaimable retries.

Revision ID: 0008_keys_escalation
Revises: 0007_same_key_retry

Adds a CHECK that every stored provider key equals the server-side
derivation from operation identity, a unique index on
(provider_account_id, provider_idempotency_key), the
RETRY_ELIGIBLE -> ESCALATED_UNRESOLVED edge (only when reclaim is
blocked, with that reason), and the evidence-triggered reopen read.
No roles or grants.

The upgrade refuses, with a clear message, if existing operations
share a provider key or hold a key that is not the derivation. The
downgrade refuses once the new edge appears in history.
"""

from alembic import op
import sqlalchemy as sa

from storage.lifecycle_sql.v0008 import (
    DERIVED_KEY,
    LEGAL_TRANSITIONS,
    downgrade_statements,
    upgrade_statements,
)


revision = "0008_keys_escalation"
down_revision = "0007_same_key_retry"
branch_labels = None
depends_on = None


def _execute(statements):
    # Same raw-connection path as 0003: composed SQL, no parameters.
    raw = op.get_bind().connection.driver_connection

    for statement in statements:
        raw.execute(statement)


def upgrade():
    connection = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("LOCK TABLE public.safeact_operations IN SHARE MODE")

    duplicates = connection.execute(sa.text("""
        SELECT provider_account_id, provider_idempotency_key, count(*)
        FROM public.safeact_operations
        WHERE provider_idempotency_key IS NOT NULL
        GROUP BY 1, 2 HAVING count(*) > 1
        ORDER BY 3 DESC LIMIT 5
    """)).fetchall()
    if duplicates:
        raise RuntimeError(
            "Upgrade refused: operations already share provider keys "
            f"(account, key, count): {duplicates}. Resolve them by hand; "
            "this migration never picks a winner."
        )

    foreign = connection.execute(sa.text(
        "SELECT count(*) FROM public.safeact_operations "
        "WHERE provider_idempotency_key IS NOT NULL "
        "AND provider_idempotency_key <> " + DERIVED_KEY
    )).scalar_one()
    if foreign:
        raise RuntimeError(
            f"Upgrade refused: {foreign} operation(s) hold a provider key "
            "that is not derived from their identity. Keys are never "
            "rewritten automatically."
        )

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
