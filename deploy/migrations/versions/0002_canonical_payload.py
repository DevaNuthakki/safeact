"""Persist canonical operation payloads and bind their digest.

Revision ID: 0002_canonical_payload
Revises: 0001_foundation
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_canonical_payload"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()

    # Fail instead of waiting indefinitely behind concurrent work.
    op.execute("SET LOCAL lock_timeout = '5s'")

    # Prevent inserts between the empty-table check
    # and the NOT NULL schema modification.
    op.execute(
        "LOCK TABLE public.safeact_operations "
        "IN ACCESS EXCLUSIVE MODE"
    )

    existing = connection.execute(
        sa.text(
            "SELECT count(*) "
            "FROM public.safeact_operations"
        )
    ).scalar_one()

    if existing != 0:
        raise RuntimeError(
            "Operations table is not empty. "
            "A reviewed backfill plan is required."
        )

    op.execute(
        "CREATE EXTENSION IF NOT EXISTS pgcrypto"
    )

    op.add_column(
        "safeact_operations",
        sa.Column(
            "payload_canonical",
            sa.Text(),
            nullable=False,
        ),
        schema="public",
    )

    op.create_check_constraint(
        "ck_safeact_operations_payload_size",
        "safeact_operations",
        "octet_length(payload_canonical) "
        "BETWEEN 2 AND 65536",
        schema="public",
    )

    op.create_check_constraint(
        "ck_safeact_operations_payload_object",
        "safeact_operations",
        "jsonb_typeof(payload_canonical::jsonb) = 'object'",
        schema="public",
    )

    op.create_check_constraint(
        "ck_safeact_operations_payload_binding",
        "safeact_operations",
        "payload_sha256 = encode("
        "digest(convert_to(payload_canonical, 'UTF8'), "
        "'sha256'), 'hex')",
        schema="public",
    )


def downgrade():
    raise RuntimeError(
        "Destructive downgrade is intentionally disabled."
    )
