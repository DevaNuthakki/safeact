"""Tenant-scoped change feed for live views with durable catch-up.

Revision ID: 0014_change_feed
Revises: 0013_review

Adds commit_xid (the writing transaction) to every audit row, with an
index, and the API-role function safeact_api_changes that lists the
operations changed between a cursor and the current snapshot's xmin.
Existing writers are unchanged. No roles or grants.

The downgrade drops the column and the function; the audit rows
themselves are kept.
"""


from alembic import op

from storage.lifecycle_sql.v0014 import (
    downgrade_statements,
    upgrade_statements,
)


revision = "0014_change_feed"
down_revision = "0013_review"
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
