"""Same-key retry from RETRY_ELIGIBLE, guarded by the database.

Revision ID: 0007_same_key_retry
Revises: 0006_claim_exit

Adds safeact_retry_basis (the single SQL definition of a protected
retry: proof for every earlier attempt, or a native key inside its
retention cutoff), the attempt budget and an accounting read. Replaces
claim (reclaim within the attempt budget and abandoned-claim cap),
begin_dispatch (recheck the basis and budget at the fence), append
(VERIFYING -> RETRY_ELIGIBLE only when protected and within budget),
the dispatch-outcome function (reason vocabulary for UNKNOWN) and the
claimable/relay scans. No roles or grants.

The downgrade restores the earlier functions and refuses once any
retry-eligible work has been reclaimed.
"""


from alembic import op
import sqlalchemy as sa

from storage.lifecycle_sql.v0007 import (
    downgrade_statements,
    upgrade_statements,
)


revision = "0007_same_key_retry"
down_revision = "0006_claim_exit"
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
        "LOCK TABLE public.safeact_operation_transitions "
        "IN ACCESS EXCLUSIVE MODE"
    )

    reclaimed = connection.execute(sa.text(
        "SELECT count(*) FROM public.safeact_operation_transitions "
        "WHERE from_state = 'RETRY_ELIGIBLE' AND to_state = 'CLAIMED'"
    )).scalar_one()
    if reclaimed:
        raise RuntimeError(
            "Retry-eligible work was reclaimed in execution history. "
            "Downgrade refused."
        )

    _execute(downgrade_statements("public"))
