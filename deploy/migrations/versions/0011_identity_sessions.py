"""Human identity and sessions verified inside PostgreSQL.

Revision ID: 0011_identity_sessions
Revises: 0010_compensation

Adds principals, bcrypt credentials, role and action grants, login
attempts, hash-only sessions with activity and revocation rows, the
internal session check, and the login, logout and whoami functions an
API role may execute. No roles or grants: in public nothing may call
them, and no principal exists.

The downgrade refuses while any principal or session exists.
"""


from alembic import op
import sqlalchemy as sa

from storage.lifecycle_sql.v0011 import (
    downgrade_statements,
    upgrade_statements,
)


revision = "0011_identity_sessions"
down_revision = "0010_compensation"
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
        "LOCK TABLE public.safeact_principals, public.safeact_sessions "
        "IN ACCESS EXCLUSIVE MODE"
    )
    history = connection.execute(sa.text(
        "SELECT (SELECT count(*) FROM public.safeact_principals)"
        " + (SELECT count(*) FROM public.safeact_sessions)"
    )).scalar_one()
    if history:
        raise RuntimeError(
            "Identity history exists. Downgrade refused."
        )

    _execute(downgrade_statements("public"))
