"""Agent keys and the MCP boundary.

Revision ID: 0016_agent_keys
Revises: 0015_review_followups

Adds administrator-issued agent keys for service principals (hash
only, scopes from {submit, investigate}, at most 30 days, revocable),
their revocations, the per-call sessions minted from a key, and the
safeact_mcp_* functions the MCP role may execute. Replaces
safeact_api_session (key-linked sessions are re-checked and narrowed to
their scope) and safeact_api_visible (a service principal sees only what
it requested). Agent submissions derive their request key and refuse a
second unsettled operation on the same target (SAT28). No roles or
grants.

The downgrade refuses while any agent key exists.
"""


from alembic import op
import sqlalchemy as sa

from storage.lifecycle_sql.v0016 import (
    downgrade_statements,
    upgrade_statements,
)


revision = "0016_agent_keys"
down_revision = "0015_review_followups"
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
        "LOCK TABLE public.safeact_agent_keys IN ACCESS EXCLUSIVE MODE"
    )
    history = connection.execute(sa.text(
        "SELECT count(*) FROM public.safeact_agent_keys"
    )).scalar_one()
    if history:
        raise RuntimeError("Agent keys exist. Downgrade refused.")

    _execute(downgrade_statements("public"))
