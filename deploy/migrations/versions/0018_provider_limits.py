"""Provider limits: 429s, long outages and retry storms (F16).

Revision ID: 0018_provider_limits
Revises: 0017_state_projection

Opt-in per contract through safeact_contract_limits. For contracts that
declare limits: every move to RETRY_ELIGIBLE records a deterministic,
jittered, capped exponential backoff (never shorter than a Retry-After)
and a reclaim before it is refused (SAT30); a per provider account
breaker opens after consecutive failing dispatch outcomes and admits one
half-open probe after its cooldown, and a token bucket bounds claims
(SAT31; refused claims append nothing); the reconciler's UNKNOWN ->
VERIFYING lookup is paced (SAT30). Adds 'ambiguous_rate_limited' to the
dispatch-outcome vocabulary and safeact_pacing for the reconciler and
the console. No roles or grants.

The downgrade refuses while any retry schedule or breaker event exists.
"""


from alembic import op
import sqlalchemy as sa

from storage.lifecycle_sql.v0018 import (
    HISTORY_TABLES,
    downgrade_statements,
    upgrade_statements,
)


revision = "0018_provider_limits"
down_revision = "0017_state_projection"
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
        if connection.execute(sa.text(
            f"SELECT count(*) FROM public.{table}"
        )).scalar_one():
            raise RuntimeError(f"{table} has history. Downgrade refused.")
    _execute(downgrade_statements("public"))
