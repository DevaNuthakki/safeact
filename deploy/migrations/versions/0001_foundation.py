"""Initial durable intent schema.

Revision ID: 0001_foundation
Revises:
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "safeact_operations",
        sa.Column("operation_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("request_key", sa.String(255), nullable=False),
        sa.Column("action_type", sa.String(128), nullable=False),
        sa.Column("resource_id", sa.String(255), nullable=False),
        sa.Column("provider_account_id", sa.String(255), nullable=False),
        sa.Column("connector_id", sa.String(128), nullable=False),
        sa.Column("contract_version", sa.String(64), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("provider_idempotency_key", sa.String(255)),
        sa.Column(
            "state", sa.String(32),
            nullable=False, server_default=sa.text("'RECORDED'"),
        ),
        sa.Column(
            "version", sa.Integer,
            nullable=False, server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "tenant_id", "request_key",
            name="uq_safeact_operations_tenant_request",
        ),
        sa.CheckConstraint(
            "payload_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_safeact_operations_digest",
        ),
        sa.CheckConstraint(
            "state = 'RECORDED' AND version = 0",
            name="ck_safeact_operations_initial_state",
        ),
        sa.CheckConstraint(
            "tenant_id <> '' AND request_key <> '' "
            "AND action_type <> '' AND resource_id <> '' "
            "AND provider_account_id <> '' "
            "AND connector_id <> '' AND contract_version <> ''",
            name="ck_safeact_operations_nonempty_identity",
        ),
    )

    op.create_table(
        "safeact_attempts",
        sa.Column("attempt_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "operation_id", UUID(as_uuid=True),
            sa.ForeignKey(
                "safeact_operations.operation_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column(
            "dispatch_risk", sa.String(32),
            nullable=False, server_default=sa.text("'PREPARED'"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "operation_id", "ordinal",
            name="uq_safeact_attempts_operation_ordinal",
        ),
        sa.CheckConstraint(
            "ordinal > 0",
            name="ck_safeact_attempts_positive_ordinal",
        ),
        sa.CheckConstraint(
            "dispatch_risk = 'PREPARED'",
            name="ck_safeact_attempts_initial_risk",
        ),
    )

    op.create_table(
        "safeact_audit_events",
        sa.Column(
            "audit_id", sa.BigInteger,
            sa.Identity(), primary_key=True,
        ),
        sa.Column(
            "operation_id", UUID(as_uuid=True),
            sa.ForeignKey(
                "safeact_operations.operation_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("details", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "safeact_outbox_events",
        sa.Column("outbox_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "operation_id", UUID(as_uuid=True),
            sa.ForeignKey(
                "safeact_operations.operation_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column(
            "state", sa.String(32),
            nullable=False, server_default=sa.text("'PENDING'"),
        ),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "state = 'PENDING'",
            name="ck_safeact_outbox_initial_state",
        ),
    )

    # Fail-closed initial release: no row may be changed
    # or deleted until protected transitions are implemented.
    op.execute("""
        CREATE FUNCTION safeact_reject_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $safeact$
        BEGIN
            RAISE EXCEPTION
                'SafeAct foundation rows are insert-only: %',
                TG_TABLE_NAME
                USING ERRCODE = 'check_violation';
            RETURN NULL;
        END;
        $safeact$;
    """)

    for table in (
        "safeact_operations",
        "safeact_attempts",
        "safeact_audit_events",
        "safeact_outbox_events",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_write_guard "
            f"BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW "
            "EXECUTE FUNCTION safeact_reject_mutation()"
        )


def downgrade():
    raise RuntimeError(
        "Destructive downgrade intentionally disabled. "
        "Use an explicitly reviewed disposable database "
        "or a separately approved recovery plan."
    )
