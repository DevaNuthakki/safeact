"""Safe compensation for the fixed workflow.

Revision ID: 0010_compensation
Revises: 0009_workflow

Adds safeact_admit_compensation and replaces the step-admission and
scan functions so the advancer can admit one separately identified
refund of exactly the charge, only after a confirmed order failure and
under the policy recorded at creation (SAT16). Its payload comes from
the durable charge record. No roles or grants.

The downgrade refuses while any compensation exists.
"""


from alembic import op
import sqlalchemy as sa

from storage.lifecycle_sql.v0010 import (
    downgrade_statements,
    upgrade_statements,
)


revision = "0010_compensation"
down_revision = "0009_workflow"
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
        "LOCK TABLE public.safeact_workflow_steps IN ACCESS EXCLUSIVE MODE"
    )
    compensations = connection.execute(sa.text(
        "SELECT count(*) FROM public.safeact_workflow_steps "
        "WHERE step = 'compensation'"
    )).scalar_one()
    if compensations:
        raise RuntimeError(
            "Compensations exist in workflow history. Downgrade refused."
        )

    _execute(downgrade_statements("public"))
