"""Durable gauges for Prometheus.

Revision ID: 0020_metrics
Revises: 0019_next_check

Adds safeact_metrics(): aggregate gauges (operations per state, the
claimable backlog and its age, open provider breakers, retries inside
their backoff) computed from the current-state projection at scrape
time, for the recovery role. Aggregates only. No tables, roles or grants.
"""


from alembic import op

from storage.lifecycle_sql.v0020 import (
    downgrade_statements,
    upgrade_statements,
)


revision = "0020_metrics"
down_revision = "0019_next_check"
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
    op.execute("SET LOCAL lock_timeout = '5s'")
    _execute(downgrade_statements("public"))
