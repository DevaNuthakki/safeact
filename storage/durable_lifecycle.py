"""Typed calls into the protected durable lifecycle functions.

Every call runs inside the caller's open transaction on a psycopg
connection authenticated as the worker or recovery role. The caller
commits. A failed or unacknowledged commit is uncertain, not a
rollback: re-observe with ``current_state`` before repeating writes.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from core.attempts import AttemptOutcome
from core.connector_profiles import profile_from_row
from core.contracts import EvidenceKind
from core.lifecycle import LifecycleState
from storage import durable_lifecycle_sql as lsql


class LifecycleRejected(Exception):
    """A protected function refused the requested change."""


class IllegalTransition(LifecycleRejected):
    pass


class StaleTransition(LifecycleRejected):
    pass


class NotClaimOwner(LifecycleRejected):
    pass


class LeaseNotExpired(LifecycleRejected):
    pass


class UnknownOperation(LifecycleRejected):
    pass


class ReclaimDisabled(LifecycleRejected):
    pass


class ClaimLeaseExpired(LifecycleRejected):
    pass


class AdmissionIncomplete(LifecycleRejected):
    pass


class EvidenceRequired(LifecycleRejected):
    pass


class ContractUnknown(LifecycleRejected):
    pass


class DeadlinePassed(LifecycleRejected):
    pass


class ReopenRefused(LifecycleRejected):
    pass


class RetryNotProtected(LifecycleRejected):
    pass


class RetryBudgetExhausted(LifecycleRejected):
    pass


class WorkflowPrecondition(LifecycleRejected):
    pass


class CompensationPrecondition(LifecycleRejected):
    pass


class NotYetDue(LifecycleRejected):
    """A retry before its not_before, or a lookup before it is due (0018)."""


class ProviderLimited(LifecycleRejected):
    """The provider's breaker is open or its dispatch rate is reached."""


class ApprovalRequired(LifecycleRejected):
    """A reviewed contract's retry needs a live approval for this seq."""


_ERRORS = {
    lsql.ILLEGAL_TRANSITION: IllegalTransition,
    lsql.STALE_TRANSITION: StaleTransition,
    lsql.NOT_CLAIM_OWNER: NotClaimOwner,
    lsql.LEASE_NOT_EXPIRED: LeaseNotExpired,
    lsql.UNKNOWN_OPERATION: UnknownOperation,
    lsql.RECLAIM_DISABLED: ReclaimDisabled,
    lsql.CLAIM_LEASE_EXPIRED: ClaimLeaseExpired,
    lsql.ADMISSION_INCOMPLETE: AdmissionIncomplete,
    lsql.EVIDENCE_REQUIRED: EvidenceRequired,
    lsql.CONTRACT_UNKNOWN: ContractUnknown,
    lsql.DEADLINE_PASSED: DeadlinePassed,
    lsql.REOPEN_REFUSED: ReopenRefused,
    lsql.RETRY_NOT_PROTECTED: RetryNotProtected,
    lsql.RETRY_BUDGET_EXHAUSTED: RetryBudgetExhausted,
    lsql.WORKFLOW_PRECONDITION: WorkflowPrecondition,
    lsql.COMPENSATION_PRECONDITION: CompensationPrecondition,
    lsql.APPROVAL_REQUIRED: ApprovalRequired,
    lsql.NOT_YET_DUE: NotYetDue,
    lsql.PROVIDER_LIMITED: ProviderLimited,
}


@dataclass(frozen=True, slots=True)
class DispatchIntent:
    """Immutable admitted intent, read under the claim's row lock."""

    tenant_id: str
    action_type: str
    resource_id: str
    provider_account_id: str
    connector_id: str
    contract_version: str
    payload_sha256: str
    payload_canonical: str
    provider_idempotency_key: str | None


@dataclass(frozen=True, slots=True)
class Claim:
    operation_id: UUID
    claim_id: UUID
    seq: int
    intent: DispatchIntent | None = None


@dataclass(frozen=True, slots=True)
class DispatchFence:
    operation_id: UUID
    claim_id: UUID
    attempt_id: UUID
    seq: int


@dataclass(frozen=True, slots=True)
class CurrentState:
    operation_id: UUID
    seq: int
    state: LifecycleState
    claim_id: UUID | None
    attempt_id: UUID | None
    actor: str | None
    lease_expires_at: datetime | None


def _call(connection, schema, function, args, *, many=False):
    if type(connection) is not psycopg.Connection:
        raise TypeError("psycopg connection required")

    if connection.info.transaction_status not in (
        psycopg.pq.TransactionStatus.IDLE,
        psycopg.pq.TransactionStatus.INTRANS,
    ):
        raise RuntimeError("Connection is not usable")

    query = sql.SQL("SELECT * FROM {}.{}({})").format(
        lsql._schema(schema),
        sql.Identifier(function),
        sql.SQL(", ").join(sql.Placeholder() for _ in args),
    )

    try:
        cursor = connection.execute(query, args)
        return cursor.fetchall() if many else cursor.fetchone()
    except psycopg.Error as error:
        rejected = _ERRORS.get(error.sqlstate)
        if rejected is not None:
            raise rejected(str(error).splitlines()[0]) from error
        raise


def _evidence(evidence):
    if evidence is None:
        evidence = {}
    if type(evidence) is not dict:
        raise TypeError("Evidence must be a JSON object")
    return Jsonb(evidence)


def claim(connection, schema, operation_id, *, lease_seconds):
    claim_id, seq, *intent = _call(
        connection, schema, "safeact_claim",
        (operation_id, lease_seconds),
    )
    return Claim(operation_id, claim_id, seq, DispatchIntent(*intent))


def begin_dispatch(connection, schema, held: Claim, *, lease_seconds):
    seq, attempt_id = _call(
        connection, schema, "safeact_begin_dispatch",
        (held.operation_id, held.seq, held.claim_id, lease_seconds),
    )
    return DispatchFence(
        held.operation_id, held.claim_id, attempt_id, seq
    )


def release_claim(connection, schema, held: Claim, *, reason):
    """The claim owner gives up before dispatch; the DB picks the exit."""

    seq, state = _call(
        connection, schema, "safeact_release_claim",
        (held.operation_id, held.seq, held.claim_id, reason),
    )
    return seq, LifecycleState(state)


def record_dispatch_outcome(
    connection, schema, fence: DispatchFence, to_state, *,
    reason, evidence=None,
):
    (seq,) = _call(
        connection, schema, "safeact_record_dispatch_outcome",
        (
            fence.operation_id, fence.seq, fence.claim_id,
            LifecycleState(to_state).value, reason,
            _evidence(evidence),
        ),
    )
    return seq


def recovery_transition(
    connection, schema, operation_id, *, expected_seq,
    expected_state, to_state, reason, evidence=None,
):
    (seq,) = _call(
        connection, schema, "safeact_recovery_transition",
        (
            operation_id, expected_seq,
            LifecycleState(expected_state).value,
            LifecycleState(to_state).value, reason,
            _evidence(evidence),
        ),
    )
    return seq


def record_attempt_outcome(
    connection, schema, attempt_id, outcome, *,
    evidence_kind=None, reference=None, details=None,
):
    (outcome_id,) = _call(
        connection, schema, "safeact_record_attempt_outcome",
        (
            attempt_id,
            AttemptOutcome(outcome).value,
            None if evidence_kind is None
            else EvidenceKind(evidence_kind).value,
            reference,
            _evidence(details),
        ),
    )
    return outcome_id


def record_outbox_delivery(connection, schema, outbox_id):
    (delivery_id,) = _call(
        connection, schema, "safeact_record_outbox_delivery",
        (outbox_id,),
    )
    return delivery_id


def claimable_operations(connection, schema, *, limit):
    rows = _call(
        connection, schema, "safeact_claimable_operations",
        (limit,), many=True,
    )
    return tuple(row[0] for row in rows)


def relay_pending(connection, schema, *, limit):
    rows = _call(
        connection, schema, "safeact_relay_pending",
        (limit,), many=True,
    )
    return tuple((row[0], row[1]) for row in rows)


@dataclass(frozen=True, slots=True)
class AttemptEvidence:
    attempt_id: UUID
    ordinal: int
    applied_reference: str | None
    not_applied_reference: str | None


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    operation_id: UUID
    seq: int
    state: LifecycleState
    tenant_id: str
    action_type: str
    resource_id: str
    provider_account_id: str
    connector_id: str
    contract_version: str
    payload_sha256: str
    provider_idempotency_key: str | None
    first_dispatch_at: datetime | None
    lease_expires_at: datetime | None
    observed_at: datetime
    attempts: tuple[AttemptEvidence, ...]


def recovery_snapshot(connection, schema, operation_id):
    row = _call(
        connection, schema, "safeact_recovery_snapshot",
        (operation_id,),
    )
    *fields, attempts = row
    fields[1] = LifecycleState(fields[1])
    return RecoverySnapshot(
        operation_id,
        *fields,
        tuple(
            AttemptEvidence(
                UUID(item["attempt_id"]),
                item["ordinal"],
                item["applied_reference"],
                item["not_applied_reference"],
            )
            for item in attempts
        ),
    )


def operations_in_state(connection, schema, state, *, limit):
    rows = _call(
        connection, schema, "safeact_operations_in_state",
        (LifecycleState(state).value, limit), many=True,
    )
    return tuple((row[0], row[1]) for row in rows)


def current_state(connection, schema, operation_id):
    row = connection.execute(
        sql.SQL(
            "SELECT operation_id, seq, state, claim_id, attempt_id, "
            "actor, lease_expires_at FROM {}.{} "
            "WHERE operation_id = %s"
        ).format(
            lsql._schema(schema),
            sql.Identifier(lsql.CURRENT_STATE_VIEW),
        ),
        (operation_id,),
    ).fetchone()

    if row is None:
        return None

    return CurrentState(
        row[0], row[1], LifecycleState(row[2]), *row[3:]
    )


def connector_contract(connection, schema, connector_id, version):
    """The registered, immutable profile a decision is bound to."""

    row = _call(
        connection, schema, "safeact_connector_contract",
        (connector_id, version),
    )
    return profile_from_row(row)


@dataclass(frozen=True, slots=True)
class InvestigationStatus:
    seq: int
    state: LifecycleState
    investigation_deadline: datetime | None
    escalated_at: datetime | None
    reopens: int
    last_lookup_at: datetime | None
    observed_at: datetime



def investigation_status(connection, schema, operation_id):
    """Deadline and reopen bookkeeping, computed by the database."""

    seq, state, *rest = _call(
        connection, schema, "safeact_investigation_status",
        (operation_id,),
    )
    return InvestigationStatus(seq, LifecycleState(state), *rest)


@dataclass(frozen=True, slots=True)
class Pacing:
    """F16 timing for one operation, computed by the database (0018).

    next_lookup_at: when the reconciler's next lookup is due (None: the
    contract declares no pacing). retry_not_before: the latest retry's
    backoff. breaker_open_until: the provider breaker, if open. limited:
    whether the contract declares limits at all.
    """

    next_lookup_at: datetime | None
    retry_not_before: datetime | None
    breaker_open_until: datetime | None
    limited: bool
    observed_at: datetime

    def lookup_due(self):
        return (self.next_lookup_at is None
                or self.next_lookup_at <= self.observed_at)


def pacing(connection, schema, operation_id):
    return Pacing(*_call(
        connection, schema, "safeact_pacing", (operation_id,),
    ))


def reclaim_blocked(connection, schema, operation_id):
    """Why retry-eligible work can no longer be reclaimed, or None."""

    (reason,) = _call(
        connection, schema, "safeact_reclaim_blocked", (operation_id,)
    )
    return reason


def reopen_evidence(connection, schema, operation_id):
    """A decisive row was recorded after the current escalation."""

    (found,) = _call(
        connection, schema, "safeact_reopen_evidence", (operation_id,)
    )
    return found


def accounting_rows(connection, schema):
    """(state, unknown_reason, count) for every admitted operation."""

    rows = _call(connection, schema, "safeact_accounting", (), many=True)
    return tuple(
        (LifecycleState(state), reason, count)
        for state, reason, count in rows
    )


def workflows_to_advance(connection, schema, *, limit):
    rows = _call(
        connection, schema, "safeact_workflows_to_advance", (limit,),
        many=True,
    )
    return tuple((row[0], row[1]) for row in rows)


def admit_workflow_step(connection, schema, workflow_id, step):
    """Admit (or return) a workflow step's operation; checked in SQL."""

    (operation_id,) = _call(
        connection, schema, "safeact_admit_workflow_step",
        (workflow_id, step),
    )
    return operation_id


def retry_approval_status(connection, schema, operation_id):
    """not_required, not_applicable, not_eligible, awaiting or approved."""

    return _call(
        connection, schema, "safeact_retry_approval_status", (operation_id,)
    )[0]


def take_reverify(connection, schema, operation_id):
    """Consume one approved extra lookup for this escalation, if any."""

    return _call(
        connection, schema, "safeact_take_reverify", (operation_id,)
    )[0]


def succeeded_by_provenance(connection, schema):
    rows = _call(
        connection, schema, "safeact_succeeded_by_provenance", (), many=True
    )
    return {provenance: count for provenance, count in rows}


def take_verification_request(connection, schema, operation_id):
    """Consume one in-bounds investigator request for this escalation."""

    return _call(
        connection, schema, "safeact_take_verification_request",
        (operation_id,),
    )[0]


def outcome_reasons(connection, schema):
    rows = _call(
        connection, schema, "safeact_outcome_reasons", (), many=True
    )
    return {(state, reason): count for state, reason, count in rows}
