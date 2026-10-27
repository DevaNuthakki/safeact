"""When the next check will happen.

Revision ID: 0019_next_check
Revises: 0018_provider_limits

Adds safeact_next_check(op): what happens next to an operation and the
earliest time it can (a not-before), with its basis, read from the same
rules the protected functions enforce (paced lookups, retry backoff,
the provider breaker, leases, approval waits, post-escalation bounds).
safeact_api_operation (0013) gains 'next_check'. No tables, roles or
grants; the downgrade restores the 0013 detail view.
"""


from alembic import op

from storage.lifecycle_sql.v0019 import (
    downgrade_statements,
    upgrade_statements,
)


revision = "0019_next_check"
down_revision = "0018_provider_limits"
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
