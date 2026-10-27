"""Handle both unique indexes during concurrent admission.

Revision ID: 0022_admission_race
Revises: 0021_workflow_replay
"""

from alembic import op

from storage.lifecycle_sql.v0022 import (
    downgrade_statements,
    upgrade_statements,
)


revision = "0022_admission_race"
down_revision = "0021_workflow_replay"
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
