"""Conventional PostgreSQL intent/outbox admission for the B3 comparison.

Ordinary application transactions and unique indexes, without SafeAct SQL.
Only throwaway baseline schemas are accepted. This is not a runnable arm yet.
"""

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID, insert

from core.contracts import canonical_payload_bytes


SCHEMA = re.compile(r"safeact_baseline_it_[0-9a-f]{32}\Z")


class Conflict(ValueError):
    """One immutable business identity was reused for a different request."""


class IncompleteAdmission(RuntimeError):
    """A persisted intent is missing its atomic initial publication/history."""


@dataclass(frozen=True)
class Request:
    tenant_id: str
    requester_id: str
    request_key: str
    connector_id: str
    contract_version: str
    provider_account_id: str
    action_type: str
    resource_id: str
    payload_canonical: str

    def __post_init__(self):
        limits = dict(tenant_id=128, requester_id=128, request_key=255,
            connector_id=64, contract_version=64, provider_account_id=128,
            action_type=128, resource_id=255)
        for name, limit in limits.items():
            value = getattr(self, name)
            if type(value) is not str or not 1 <= len(value) <= limit or any(c.isspace() or ord(c) < 32 for c in value):
                raise ValueError(f"Invalid baseline {name}")
        if type(self.payload_canonical) is not str:
            raise ValueError("A canonical payload is required")
        try:
            canonical = canonical_payload_bytes(json.loads(self.payload_canonical)).decode()
        except (ValueError, TypeError) as error:
            raise ValueError("Invalid canonical payload") from error
        if canonical != self.payload_canonical or len(canonical.encode()) > 65536:
            raise ValueError("Payload must be canonical and bounded")

    @classmethod
    def from_payload(cls, *, payload, **fields):
        return cls(payload_canonical=canonical_payload_bytes(payload).decode(), **fields)

    def immutable(self):
        return dict(self.__dict__, payload_sha256=sha256(self.payload_canonical.encode()).hexdigest())


@dataclass(frozen=True)
class Receipt:
    operation_id: UUID
    created: bool


class Tables:
    def __init__(self, schema):
        if type(schema) is not str or not SCHEMA.fullmatch(schema):
            raise ValueError("Only an owned disposable baseline schema is allowed")
        self.schema = schema
        self.metadata = sa.MetaData(schema=schema)
        self.jobs = sa.Table("baseline_jobs", self.metadata,
            sa.Column("operation_id", PgUUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("requester_id", sa.String(128), nullable=False),
            sa.Column("request_key", sa.String(255), nullable=False),
            sa.Column("connector_id", sa.String(64), nullable=False),
            sa.Column("contract_version", sa.String(64), nullable=False),
            sa.Column("provider_account_id", sa.String(128), nullable=False),
            sa.Column("action_type", sa.String(128), nullable=False),
            sa.Column("resource_id", sa.String(255), nullable=False),
            sa.Column("payload_canonical", sa.Text, nullable=False),
            sa.Column("payload_sha256", sa.String(64), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="RECORDED"),
            sa.Column("version", sa.Integer, nullable=False, server_default="0"),
            sa.Column("claim_token", PgUUID(as_uuid=True)),
            sa.Column("lease_until", sa.DateTime(timezone=True)),
            sa.Column("abandoned_claims", sa.Integer, nullable=False, server_default="0"),
            sa.Column("first_fence_at", sa.DateTime(timezone=True)),
            sa.Column("not_before", sa.DateTime(timezone=True)),
            sa.Column("next_lookup_at", sa.DateTime(timezone=True)),
            sa.Column("lookup_token", PgUUID(as_uuid=True)),
            sa.Column("lookup_lease_until", sa.DateTime(timezone=True)),
            sa.Column("lookup_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("escalated_at", sa.DateTime(timezone=True)),
            sa.Column("reopens", sa.Integer, nullable=False, server_default="0"),
            sa.Column("resolved_at", sa.DateTime(timezone=True)),
            sa.Column("evidence_pending", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "request_key", name="baseline_jobs_identity"),
            sa.CheckConstraint("version >= 0", name="baseline_jobs_version"),
            sa.CheckConstraint("payload_sha256 = encode(sha256(convert_to(payload_canonical, 'UTF8')), 'hex')",
                               name="baseline_jobs_payload_hash"),
            sa.CheckConstraint("status IN ('RECORDED', 'CLAIMED', 'DISPATCHING', 'UNKNOWN', 'VERIFYING', "
                               "'RETRY_ELIGIBLE', 'SUCCEEDED', 'FAILED_CONFIRMED', 'ESCALATED_UNRESOLVED')",
                               name="baseline_jobs_status"))
        self.outbox = sa.Table("baseline_outbox", self.metadata,
            sa.Column("outbox_id", PgUUID(as_uuid=True), primary_key=True),
            sa.Column("operation_id", PgUUID(as_uuid=True), sa.ForeignKey(self.jobs.c.operation_id), nullable=False),
            sa.Column("ordinal", sa.Integer, nullable=False),
            sa.Column("published_at", sa.DateTime(timezone=True)),
            sa.Column("publication_token", PgUUID(as_uuid=True)),
            sa.Column("publication_lease_until", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("operation_id", "ordinal", name="baseline_outbox_identity"))
        self.audit = sa.Table("baseline_audit", self.metadata,
            sa.Column("audit_id", sa.BigInteger, primary_key=True, autoincrement=True),
            sa.Column("operation_id", PgUUID(as_uuid=True), sa.ForeignKey(self.jobs.c.operation_id), nullable=False),
            sa.Column("kind", sa.String(64), nullable=False),
            sa.Column("version", sa.Integer, nullable=False),
            sa.Column("details", JSONB, nullable=False),
            sa.Column("commit_xid", sa.String(32), nullable=False,
                      server_default=sa.text("pg_current_xact_id()::text")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
        self.attempts = sa.Table("baseline_attempts", self.metadata,
            sa.Column("attempt_id", PgUUID(as_uuid=True), primary_key=True),
            sa.Column("operation_id", PgUUID(as_uuid=True), sa.ForeignKey(self.jobs.c.operation_id), nullable=False),
            sa.Column("ordinal", sa.Integer, nullable=False),
            sa.Column("claim_token", PgUUID(as_uuid=True), nullable=False),
            sa.Column("fenced_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("fenced_version", sa.Integer, nullable=False),
            sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("operation_id", "ordinal", name="baseline_attempt_ordinal"),
            sa.UniqueConstraint("claim_token", name="baseline_attempt_claim"))
        self.observations = sa.Table("baseline_observations", self.metadata,
            sa.Column("observation_id", PgUUID(as_uuid=True), primary_key=True),
            sa.Column("operation_id", PgUUID(as_uuid=True), sa.ForeignKey(self.jobs.c.operation_id), nullable=False),
            sa.Column("attempt_id", PgUUID(as_uuid=True), sa.ForeignKey(self.attempts.c.attempt_id)),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("kind", sa.String(32), nullable=False),
            sa.Column("reference", sa.Text),
            sa.Column("retryable", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("details", JSONB, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("source IN ('dispatch', 'lookup')", name="baseline_observation_source"),
            sa.CheckConstraint("kind IN ('applied', 'not_applied', 'ambiguous', 'not_found', 'inconclusive')",
                               name="baseline_observation_kind"),
            sa.CheckConstraint("(kind IN ('applied', 'not_applied')) = (reference IS NOT NULL)",
                               name="baseline_observation_reference"))
        self.accounts = sa.Table("baseline_accounts", self.metadata,
            sa.Column("connector_id", sa.String(64), primary_key=True),
            sa.Column("provider_account_id", sa.String(128), primary_key=True),
            sa.Column("tokens", sa.Float, nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("failures", sa.Integer, nullable=False, server_default="0"),
            sa.Column("open_until", sa.DateTime(timezone=True)),
            sa.Column("probe_token", PgUUID(as_uuid=True)),
            sa.Column("probe_until", sa.DateTime(timezone=True)))
        self.deliveries = sa.Table("baseline_deliveries", self.metadata,
            sa.Column("delivery_id", PgUUID(as_uuid=True), primary_key=True),
            sa.Column("outbox_id", PgUUID(as_uuid=True), sa.ForeignKey(self.outbox.c.outbox_id), nullable=False),
            sa.Column("operation_id", PgUUID(as_uuid=True), sa.ForeignKey(self.jobs.c.operation_id), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
        from chaos.baselines.schema import extend
        extend(self)


def admit(engine, tables, request, *, after_commit=None):
    """Persist intent/history/publication atomically; recover by exact identity.

    The caller's authentication boundary supplies requester/tenant. There is no
    provider call here. `after_commit` is a harness-only lost-ack hook; a raised
    acknowledgement error does not imply that the transaction rolled back.
    """
    if not isinstance(tables, Tables) or not isinstance(request, Request):
        raise TypeError("Typed baseline tables and request required")
    with engine.begin() as connection:
        receipt = admit_in(connection,tables,request)
    if after_commit is not None:
        after_commit(receipt)
    return receipt


def admit_in(connection, tables, request):
    """The same admission bundle, inside a workflow-owned transaction."""
    if not isinstance(tables, Tables) or not isinstance(request, Request):
        raise TypeError("Typed baseline tables and request required")
    if connection.get_isolation_level() != "READ COMMITTED":
        raise ValueError("Baseline admission requires READ COMMITTED isolation")
    expected, jobs = request.immutable(), tables.jobs
    operation_id = connection.execute(insert(jobs).values(
        operation_id=uuid4(), **expected).on_conflict_do_nothing().returning(jobs.c.operation_id)).scalar_one_or_none()
    created = operation_id is not None
    if created:
        connection.execute(tables.audit.insert().values(operation_id=operation_id,
            kind="INTENT_RECORDED", version=0, details=dict(requester_id=request.requester_id)))
        connection.execute(tables.outbox.insert().values(outbox_id=uuid4(), operation_id=operation_id, ordinal=0))
    else:
        existing = connection.execute(sa.select(jobs).where(
            jobs.c.tenant_id==request.tenant_id, jobs.c.request_key==request.request_key)).mappings().one_or_none()
        if existing is None or any(existing[k]!=v for k,v in expected.items()):
            raise Conflict("Business identity conflicts with the original immutable request")
        operation_id = existing["operation_id"]
        initial_audit = connection.scalar(sa.select(sa.func.count()).select_from(tables.audit).where(
            tables.audit.c.operation_id==operation_id, tables.audit.c.kind=="INTENT_RECORDED"))
        initial_outbox = connection.scalar(sa.select(sa.func.count()).select_from(tables.outbox).where(
            tables.outbox.c.operation_id==operation_id, tables.outbox.c.ordinal==0))
        if initial_audit!=1 or initial_outbox!=1:
            raise IncompleteAdmission("Original intent does not have one complete initial bundle")
    return Receipt(operation_id,created)
