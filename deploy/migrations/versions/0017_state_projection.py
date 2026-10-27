"""Current-state projection.

Revision ID: 0017_state_projection
Revises: 0016_agent_keys

Adds safeact_state_projection: one row per operation holding its latest
transition (RECORDED at seq 0 before any). It is written only in the
protected path and in the same transaction: an AFTER INSERT trigger on
safeact_operations adds the RECORDED row, and safeact_lifecycle_append
moves it to each transition it inserts. A guard trigger refuses any
write that does not match the log (SAT29), and safeact_lifecycle_lock
refuses when the projection's seq differs from the log's. The 0003
current-state view keeps its columns and reads the projection; the
claimable, relay and in-state scans read its state index instead of
anti-joining the log. The upgrade locks the log, backfills from it and
then creates the guard. No roles or grants.

The downgrade restores every replaced text and drops the projection.
The transition log is never touched, so nothing is lost.
"""


from alembic import op

from storage.lifecycle_sql.v0017 import (
    downgrade_statements,
    upgrade_statements,
)


revision = "0017_state_projection"
down_revision = "0016_agent_keys"
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
