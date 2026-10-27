"""Durable SafeAct intent submission.

This module records internal intent only.

It does not authenticate API callers, authorize business
actions, enqueue jobs, or dispatch provider requests.

A trusted application layer must enforce identity,
tenant permissions, and action authorization before
calling this service.

The database remains insert-only.
"""

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine

from core.contracts import canonical_payload_bytes
from core.provider_keys import derive_provider_key
from storage.schema import (
    audit_events,
    operations,
    outbox_events,
)


class SubmissionValidationError(ValueError):
    """The submitted intent violates local input contracts."""


class _Refusal:
    """Carries the unique violation PostgreSQL reported, if any."""

    def __init__(self, message, *, sqlstate=None, constraint=None):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.constraint = constraint


class SubmissionConflict(_Refusal, ValueError):
    """A tenant request key belongs to a different immutable action."""


class SubmissionStateUncertain(_Refusal, RuntimeError):
    """Database state could not be reconciled conclusively."""


UNIQUE_VIOLATION = "23505"
REQUEST_KEY_CONSTRAINT = "uq_safeact_operations_tenant_request"


@dataclass(frozen=True, slots=True)
class IntentSubmission:
    tenant_id: str
    request_key: str
    action_type: str
    resource_id: str
    provider_account_id: str
    connector_id: str
    contract_version: str
    actor_id: str
    payload: dict[str, Any]
    provider_idempotency_key: str | None = None

    def __post_init__(self) -> None:
        limits = {
            "tenant_id": 128,
            "request_key": 255,
            "action_type": 128,
            "resource_id": 255,
            "provider_account_id": 255,
            "connector_id": 128,
            "contract_version": 64,
            "actor_id": 128,
        }

        for name, limit in limits.items():
            value = getattr(self, name)

            if (
                type(value) is not str
                or not value
                or len(value) > limit
                or value != value.strip()
                or any(
                    ord(char) < 32 or ord(char) == 127
                    for char in value
                )
            ):
                raise SubmissionValidationError(
                    f"Invalid {name}"
                )

        # Provider keys are derived server-side from operation
        # identity. A caller may omit the key or repeat the derived
        # value; any other value is refused, never stored.
        derived = derive_provider_key(
            self.tenant_id, self.request_key,
            self.connector_id, self.provider_account_id,
        )
        if self.provider_idempotency_key not in (None, derived):
            raise SubmissionValidationError(
                "Provider idempotency keys are derived server-side"
            )
        object.__setattr__(self, "provider_idempotency_key", derived)

        if type(self.payload) is not dict:
            raise SubmissionValidationError(
                "Payload must be a JSON object"
            )


@dataclass(frozen=True, slots=True)
class SubmissionReceipt:
    operation_id: UUID
    created: bool



class IntentObservationStatus(str, Enum):
    """Database admission evidence, never provider-side evidence."""

    CONFIRMED_RECORDED = "CONFIRMED_RECORDED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class IntentObservation:
    status: IntentObservationStatus
    operation_id: UUID | None = None


def _immutable_identity(
    request: IntentSubmission,
    wire: str,
    digest: str,
) -> dict[str, Any]:
    """One identity definition for writes and reconciliation."""
    return {
        "tenant_id": request.tenant_id,
        "request_key": request.request_key,
        "action_type": request.action_type,
        "resource_id": request.resource_id,
        "provider_account_id": request.provider_account_id,
        "connector_id": request.connector_id,
        "contract_version": request.contract_version,
        "payload_sha256": digest,
        "payload_canonical": wire,
        "provider_idempotency_key":
            request.provider_idempotency_key,
    }


def _prepare_payload(
    payload: dict[str, Any],
) -> tuple[str, str]:
    """Derive storage text and digest from one canonical byte string.

    Raw caller-supplied canonical strings and hashes are
    never accepted here.
    """
    try:
        encoded = canonical_payload_bytes(payload)
        wire = encoded.decode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise SubmissionValidationError(
            "Invalid restricted JSON payload"
        ) from exc

    if not 2 <= len(encoded) <= 65536:
        raise SubmissionValidationError(
            "Canonical payload exceeds storage limits"
        )

    return wire, sha256(encoded).hexdigest()



def _initial_bundle_verified(
    connection: Connection,
    operation_id: UUID,
) -> bool:
    """Verify exactly one valid initial audit and outbox event.

    This is database admission evidence only.
    It must never be interpreted as provider-side proof.
    """
    audit_rows = connection.execute(
        sa.select(audit_events).where(
            audit_events.c.operation_id == operation_id,
            audit_events.c.event_type == "INTENT_RECORDED",
        )
    ).mappings().all()

    outbox_rows = connection.execute(
        sa.select(outbox_events).where(
            outbox_events.c.operation_id == operation_id,
            outbox_events.c.event_type
            == "INTENT_RECORDED_NO_DISPATCH",
        )
    ).mappings().all()

    if len(audit_rows) != 1 or len(outbox_rows) != 1:
        return False

    audit = audit_rows[0]
    outbox = outbox_rows[0]

    details = audit["details"]
    payload = outbox["payload"]

    return (
        isinstance(details, dict)
        and type(details.get("schema_version")) is int
        and details["schema_version"] == 1
        and details.get("dispatch_authorized") is False
        and isinstance(payload, dict)
        and payload.get("dispatch_enabled") is False
        and outbox["state"] == "PENDING"
    )


def _assert_original_actor(
    connection: Connection,
    operation_id: UUID,
    expected_actor_id: str,
) -> None:
    """Reject reuse of an existing key by another actor.

    The original actor is recorded in the immutable
    INTENT_RECORDED audit event. Ambiguous evidence is
    not treated as authorization.
    """
    actors = connection.execute(
        sa.select(audit_events.c.actor_id).where(
            audit_events.c.operation_id == operation_id,
            audit_events.c.event_type == "INTENT_RECORDED",
        )
    ).scalars().all()

    if len(actors) != 1:
        raise SubmissionStateUncertain(
            "Original admission actor evidence is ambiguous"
        )

    if actors[0] != expected_actor_id:
        raise SubmissionConflict(
            "Request key is owned by a different actor"
        )


def _refuse_skipped_conflict(
    connection: Connection,
    operation_id: UUID,
    identity: dict[str, Any],
) -> None:
    """Have PostgreSQL name the conflict ON CONFLICT DO NOTHING skipped.

    Reached only when no row with this tenant/request key is
    visible, so the conflict is not a replay. The same insert
    without ON CONFLICT, in a savepoint that is always rolled
    back, raises the real unique violation. Nothing is created.
    """
    probe = connection.begin_nested()
    try:
        connection.execute(
            sa.insert(operations).values(
                operation_id=operation_id,
                **identity,
            )
        )
    except sa.exc.IntegrityError as error:
        sqlstate = getattr(error.orig, "sqlstate", None)
        if sqlstate != UNIQUE_VIOLATION:
            raise
        constraint = error.orig.diag.constraint_name
        if constraint == REQUEST_KEY_CONSTRAINT:
            # The row exists but this snapshot cannot see it.
            raise SubmissionStateUncertain(
                "Conflicting operation is not visible",
                sqlstate=sqlstate, constraint=constraint,
            ) from error
        raise SubmissionConflict(
            "Admission conflicts with a different operation",
            sqlstate=sqlstate, constraint=constraint,
        ) from error
    finally:
        if probe.is_active:
            probe.rollback()

    # The skipped conflict is gone; a fresh admission decides.
    raise SubmissionStateUncertain(
        "Conflicting operation is no longer present"
    )


def record_intent(
    connection: Connection,
    request: IntentSubmission,
) -> SubmissionReceipt:
    """Record an intent within an existing DB transaction.

    A nested savepoint prevents partial operation/audit/
    outbox bundles even if the outer caller catches an
    exception and later commits its transaction.

    The caller owns the outer transaction and its commit.
    """

    if not isinstance(connection, Connection):
        raise TypeError(
            "A SQLAlchemy Connection is required"
        )

    if not connection.in_transaction():
        raise RuntimeError(
            "An explicit outer transaction is required"
        )

    if not isinstance(request, IntentSubmission):
        raise TypeError(
            "An IntentSubmission is required"
        )

    wire, digest = _prepare_payload(request.payload)

    identity = _immutable_identity(
        request, wire, digest
    )

    # The database unique constraint serializes competing
    # submissions for the same tenant/request key.
    # ON CONFLICT never modifies an existing operation.
    new_id = uuid4()
    with connection.begin_nested():
        operation_id = connection.execute(
            pg_insert(operations)
            .values(
                operation_id=new_id,
                **identity,
            )
            # Untargeted: identical concurrent admissions may meet on
            # the derived provider key index before the request key
            # index (0022). Which constraint fired is proven below.
            .on_conflict_do_nothing()
            .returning(operations.c.operation_id)
        ).scalar_one_or_none()

        if operation_id is not None:
            connection.execute(
                sa.insert(audit_events).values(
                    operation_id=operation_id,
                    event_type="INTENT_RECORDED",
                    actor_id=request.actor_id,
                    details={
                        "schema_version": 1,
                        "dispatch_authorized": False,
                    },
                )
            )

            connection.execute(
                sa.insert(outbox_events).values(
                    outbox_id=uuid4(),
                    operation_id=operation_id,
                    event_type=(
                        "INTENT_RECORDED_NO_DISPATCH"
                    ),
                    payload={
                        "dispatch_enabled": False,
                    },
                )
            )

            return SubmissionReceipt(
                operation_id=operation_id,
                created=True,
            )

        existing = connection.execute(
            sa.select(operations).where(
                operations.c.tenant_id == request.tenant_id,
                operations.c.request_key == request.request_key,
            )
        ).mappings().one_or_none()

        # A missing row after a conflict must not be
        # interpreted as permission to create another
        # action or trigger provider dispatch.
        if existing is None:
            _refuse_skipped_conflict(connection, new_id, identity)

        # The visible row holds this tenant/request key, so the
        # request key constraint is violated. Every identity field,
        # including the derived provider key, must match for the
        # conflict to be a replay of that one row.
        for field, expected in identity.items():
            if existing[field] != expected:
                raise SubmissionConflict(
                    "Request key reused for a different "
                    "immutable operation",
                    sqlstate=UNIQUE_VIOLATION,
                    constraint=REQUEST_KEY_CONSTRAINT,
                )

        if (
            existing["state"] != "RECORDED"
            or existing["version"] != 0
            or not _initial_bundle_verified(
                connection,
                existing["operation_id"],
            )
        ):
            raise SubmissionStateUncertain(
                "Matching operation lacks a verified "
                "initial admission bundle"
            )

        _assert_original_actor(
            connection,
            existing["operation_id"],
            request.actor_id,
        )

        return SubmissionReceipt(
            operation_id=existing["operation_id"],
            created=False,
        )



def observe_intent(
    engine: Engine,
    request: IntentSubmission,
) -> IntentObservation:
    """Observe a committed initial admission bundle.

    CONFIRMED_RECORDED proves only the initial matching
    operation/audit/outbox records are visible. It does
    not prove a provider action was dispatched or applied.

    UNKNOWN includes absent or incomplete observations.
    An absent row cannot rule out an in-flight late commit.

    This is a read-only observation. It never retries,
    inserts, authorizes, or dispatches an external action.
    """
    if not isinstance(engine, Engine):
        raise TypeError("A SQLAlchemy Engine is required")

    if not isinstance(request, IntentSubmission):
        raise TypeError("An IntentSubmission is required")

    wire, digest = _prepare_payload(request.payload)
    identity = _immutable_identity(request, wire, digest)

    unknown = IntentObservation(
        status=IntentObservationStatus.UNKNOWN,
    )

    try:
        with engine.connect() as connection:
            operation = connection.execute(
                sa.select(operations).where(
                    operations.c.tenant_id
                    == request.tenant_id,
                    operations.c.request_key
                    == request.request_key,
                )
            ).mappings().one_or_none()

            if operation is None:
                return unknown

            for field, expected in identity.items():
                if operation[field] != expected:
                    raise SubmissionConflict(
                        "Request key belongs to a different "
                        "immutable operation"
                    )

            if (
                operation["state"] != "RECORDED"
                or operation["version"] != 0
            ):
                return unknown

            if not _initial_bundle_verified(
                connection,
                operation["operation_id"],
            ):
                return unknown

            _assert_original_actor(
                connection,
                operation["operation_id"],
                request.actor_id,
            )

            return IntentObservation(
                status=(
                    IntentObservationStatus.CONFIRMED_RECORDED
                ),
                operation_id=operation["operation_id"],
            )

    except sa.exc.SQLAlchemyError as exc:
        raise SubmissionStateUncertain(
            "Database admission observation unavailable"
        ) from exc


def submit_intent(
    engine: Engine,
    request: IntentSubmission,
) -> SubmissionReceipt:
    """Commit an operation, audit, and outbox atomically.

    Database connection or commit failures are propagated.
    A caller must not interpret such failures as proof that
    nothing committed.

    Recovery must reconcile the same tenant/request key
    before deciding on any further action.
    """

    with engine.begin() as connection:
        return record_intent(connection, request)
