"""Initial SafeAct persistence schema.

Version 0001 records intent but does not permit
operation dispatch or mutable workflow transitions.

The Alembic revision is the authoritative migration.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


metadata = sa.MetaData()


operations = sa.Table(
    "safeact_operations",
    metadata,
    sa.Column("operation_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("tenant_id", sa.String(128), nullable=False),
    sa.Column("request_key", sa.String(255), nullable=False),
    sa.Column("action_type", sa.String(128), nullable=False),
    sa.Column("resource_id", sa.String(255), nullable=False),
    sa.Column("provider_account_id", sa.String(255), nullable=False),
    sa.Column("connector_id", sa.String(128), nullable=False),
    sa.Column("contract_version", sa.String(64), nullable=False),
    sa.Column("payload_sha256", sa.String(64), nullable=False),
    sa.Column("payload_canonical", sa.Text, nullable=False),
    sa.Column("provider_idempotency_key", sa.String(255)),
    sa.Column(
        "state",
        sa.String(32),
        nullable=False,
        server_default=sa.text("'RECORDED'"),
    ),
    sa.Column(
        "version",
        sa.Integer,
        nullable=False,
        server_default=sa.text("0"),
    ),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    ),
    sa.UniqueConstraint(
        "tenant_id",
        "request_key",
        name="uq_safeact_operations_tenant_request",
    ),
    sa.CheckConstraint(
        "payload_sha256 ~ '^[0-9a-f]{64}$'",
        name="ck_safeact_operations_digest",
    ),
    sa.CheckConstraint(
        "octet_length(payload_canonical) BETWEEN 2 AND 65536",
        name="ck_safeact_operations_payload_size",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(payload_canonical::jsonb) = 'object'",
        name="ck_safeact_operations_payload_object",
    ),
    sa.CheckConstraint(
        "payload_sha256 = encode("
        "digest(convert_to(payload_canonical, 'UTF8'), 'sha256'),"
        "'hex')",
        name="ck_safeact_operations_payload_binding",
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


attempts = sa.Table(
    "safeact_attempts",
    metadata,
    sa.Column("attempt_id", UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "operation_id",
        UUID(as_uuid=True),
        sa.ForeignKey(
            "safeact_operations.operation_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    ),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column(
        "dispatch_risk",
        sa.String(32),
        nullable=False,
        server_default=sa.text("'PREPARED'"),
    ),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    ),
    sa.UniqueConstraint(
        "operation_id",
        "ordinal",
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


audit_events = sa.Table(
    "safeact_audit_events",
    metadata,
    sa.Column(
        "audit_id",
        sa.BigInteger,
        sa.Identity(),
        primary_key=True,
    ),
    sa.Column(
        "operation_id",
        UUID(as_uuid=True),
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
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    ),
)


outbox_events = sa.Table(
    "safeact_outbox_events",
    metadata,
    sa.Column("outbox_id", UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "operation_id",
        UUID(as_uuid=True),
        sa.ForeignKey(
            "safeact_operations.operation_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    ),
    sa.Column("event_type", sa.String(128), nullable=False),
    sa.Column(
        "state",
        sa.String(32),
        nullable=False,
        server_default=sa.text("'PENDING'"),
    ),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    ),
    sa.CheckConstraint(
        "state = 'PENDING'",
        name="ck_safeact_outbox_initial_state",
    ),
)
