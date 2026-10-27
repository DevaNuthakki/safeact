"""Bind workflow replay to the original shipping charge.

Revision ID: 0021_workflow_replay
Revises: 0020_metrics
"""

from alembic import op

from storage.lifecycle_sql.v0021 import (
    downgrade_statements,
    upgrade_statements,
)


revision = "0021_workflow_replay"
down_revision = "0020_metrics"
branch_labels = None
depends_on = None


def _execute(statements):
    raw = op.get_bind().connection.driver_connection
    for statement in statements:
        raw.execute(statement)


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    _execute(upgrade_statements("public"))


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    _execute(downgrade_statements("public"))
