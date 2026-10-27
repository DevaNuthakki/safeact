"""Durable relay and execution worker.

Experimental and disposable-schema only. The only reachable providers
are the in-process mocks behind ``connectors``; production execution
is not approved.

Runtime independence: the relay publishes through a ``Publisher`` and
workers consume through a ``TaskSource``. Milestone 1 runs both
in-process (``InMemoryTaskQueue``). Celery + Redis are deferred to a
later milestone but remain required in v1; they plug in as another
Publisher/TaskSource without changing this logic.

Ordering per task:
1. claim (RECORDED -> CLAIMED), returning the durable intent
2. validate that intent; refuse to dispatch anything malformed or
   anything no registered connector accepts
3. commit the dispatch-risk fence (CLAIMED -> DISPATCHING)
4. only after that commit is acknowledged, dispatch through the
   connector, which classifies the response under its contract
5. record attempt evidence and the outcome in one transaction

A failed or unacknowledged commit is uncertain, never a rollback. The
worker then never calls the provider; after the outcome write it
re-observes by operation identity before repeating anything.
"""

import json
import logging

from collections import deque
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from threading import Lock
from time import monotonic
from typing import Protocol
from uuid import UUID

import psycopg

from core.attempts import AttemptOutcome
from core.contracts import (
    EvidenceKind,
    canonical_payload_bytes,
    canonical_payload_hash,
)
from core.lifecycle import RETRY_PENDING_REASON, LifecycleState as S
from connectors.base import DispatchKind
from observability import metrics
from storage import durable_lifecycle as lifecycle

# Raised by a commit or by a lost acknowledgement after it.
UNCERTAIN_ERRORS = (
    psycopg.OperationalError,
    psycopg.InterfaceError,
    ConnectionError,
    OSError,
)

@dataclass(frozen=True, slots=True)
class ExecutionTask:
    operation_id: UUID
    outbox_id: UUID
    delivery_id: UUID


class Publisher(Protocol):
    def publish(self, task: ExecutionTask) -> None: ...


class TaskSource(Protocol):
    def next_task(self) -> ExecutionTask | None: ...


class InMemoryTaskQueue:
    """In-process Publisher and TaskSource for Milestone 1."""

    def __init__(self):
        self._lock = Lock()
        self._tasks = deque()
        self.published = 0

    def publish(self, task):
        with self._lock:
            self._tasks.append(task)
            self.published += 1

    def next_task(self):
        with self._lock:
            return self._tasks.popleft() if self._tasks else None

    def __len__(self):
        with self._lock:
            return len(self._tasks)


log = logging.getLogger("safeact.worker")


def relay_once(connection, schema, publisher, *, limit=100):
    """Publish every never-claimed outbox event once more.

    Deliveries are recorded before publishing, so a crash between the
    two leaves the event pending and the next pass republishes it.
    Duplicates are expected; the claim makes them harmless.
    """

    pending = lifecycle.relay_pending(connection, schema, limit=limit)
    connection.commit()

    published = []
    for outbox_id, operation_id in pending:
        delivery_id = lifecycle.record_outbox_delivery(
            connection, schema, outbox_id
        )
        connection.commit()

        task = ExecutionTask(operation_id, outbox_id, delivery_id)
        publisher.publish(task)
        published.append(task)

    metrics.RELAYED.inc(len(published))
    return tuple(published)


class ExecutionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED_CONFIRMED = "failed_confirmed"
    UNKNOWN = "unknown"
    NOT_CLAIMABLE = "not_claimable"
    INVALID_INTENT = "invalid_intent"
    UNCERTAIN = "uncertain"
    LATE_EVIDENCE = "late_evidence"


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    operation_id: UUID
    status: ExecutionStatus
    provider_called: bool
    stage: str


_FROM_STATE = {
    S.SUCCEEDED: ExecutionStatus.SUCCEEDED,
    S.FAILED_CONFIRMED: ExecutionStatus.FAILED_CONFIRMED,
    S.UNKNOWN: ExecutionStatus.UNKNOWN,
}


class _Uncertain(Exception):
    def __init__(self, stage):
        super().__init__(stage)
        self.stage = stage


@dataclass(frozen=True, slots=True)
class _Outcome:
    state: S
    attempt_outcome: AttemptOutcome
    evidence_kind: EvidenceKind | None
    reference: str | None
    reason: str
    # False only when the worker never sent the request.
    sent: bool = True
    # Non-decisive provider facts (for example a refund's status),
    # written with the evidence and never read by a decision.
    facts: tuple = ()

    def details(self, **extra):
        found = dict(extra)
        if self.facts:
            found["provider"] = dict(self.facts)
        return found or None


def connector_map(connectors):
    """Index connectors by id; one adapter per connector id."""

    mapped = {}
    for connector in connectors:
        if connector.connector_id in mapped:
            raise ValueError("duplicate connector")
        mapped[connector.connector_id] = connector
    return mapped


def valid_intent(intent, connector):
    """The durable intent must be well formed for its connector."""

    if (
        intent is None
        or connector is None
        or not connector.accepts(intent)
    ):
        return False

    try:
        payload = json.loads(intent.payload_canonical)
        return (
            canonical_payload_bytes(payload).decode("utf-8")
            == intent.payload_canonical
            and canonical_payload_hash(payload) == intent.payload_sha256
        )
    except (ValueError, TypeError):
        return False


def fence_actor(connection):
    return connection.execute("SELECT session_user").fetchone()[0]


class ExecutionWorker:

    def __init__(
        self, *, connect, schema, connectors,
        lease_seconds=60, fault_hook=None,
        connection_scope=None,
    ):
        self._connect = connect
        self._connection_scope = connection_scope or (lambda: closing(connect()))
        self._schema = schema
        self._connectors = connector_map(connectors)
        self._lease = lease_seconds
        # Test-only fault injection: hook(point, connection).
        self._fault = fault_hook or (lambda point, connection: None)

    def _commit(self, connection, stage):
        try:
            self._fault(f"before_{stage}_commit", connection)
            connection.commit()
            self._fault(f"after_{stage}_commit", connection)
        except UNCERTAIN_ERRORS as error:
            raise _Uncertain(stage) from error

    def run_until_idle(self, source):
        results = []
        while (task := source.next_task()) is not None:
            results.append(self.execute(task))
        return results

    def execute(self, task):
        with self._connection_scope() as connection:
            return self._execute(connection, task)

    def _execute(self, connection, task):
        operation_id = task.operation_id
        schema = self._schema

        def result(status, stage, called=False):
            return ExecutionResult(operation_id, status, called, stage)

        # 1. Claim. Duplicate deliveries stop here.
        try:
            held = lifecycle.claim(
                connection, schema, operation_id,
                lease_seconds=self._lease,
            )
            self._commit(connection, "claim")
        except lifecycle.LifecycleRejected as refused:
            connection.rollback()
            metrics.CLAIMS_REFUSED.labels(
                metrics.refused_reason(refused)).inc()
            return result(ExecutionStatus.NOT_CLAIMABLE, "claim")
        except _Uncertain:
            return result(ExecutionStatus.UNCERTAIN, "claim")

        # 2. Validate the durable intent before taking dispatch risk.
        connector = self._connectors.get(held.intent.connector_id)
        if not valid_intent(held.intent, connector):
            return result(ExecutionStatus.INVALID_INTENT, "validate")

        # 3. Commit the dispatch-risk fence. The request deadline runs
        # from before the fence call: the contract's key safety margin
        # covers it, so a request that passes the fence just before
        # the retention cutoff is still sent inside the window.
        try:
            profile = lifecycle.connector_contract(
                connection, schema,
                held.intent.connector_id, held.intent.contract_version,
            )
            started = monotonic()
            fence = lifecycle.begin_dispatch(
                connection, schema, held, lease_seconds=self._lease,
            )
            self._commit(connection, "fence")
        except (
            lifecycle.ContractUnknown,
            lifecycle.RetryNotProtected,
            lifecycle.RetryBudgetExhausted,
        ) as refused:
            # Refused before any dispatch: give the claim up at once
            # rather than holding it until the lease expires. The
            # database picks the exit from the attempt history.
            connection.rollback()
            self._release(
                connection, held,
                "fence_refused_" + type(refused).__name__.lower(),
            )
            return result(ExecutionStatus.NOT_CLAIMABLE, "fence")
        except lifecycle.LifecycleRejected:
            connection.rollback()
            return result(ExecutionStatus.NOT_CLAIMABLE, "fence")
        except _Uncertain:
            # The fence may or may not exist. Never dispatch on doubt.
            return result(ExecutionStatus.UNCERTAIN, "fence")

        # 4. Provider call, only after the fence commit returned, and
        # only while the request deadline has not elapsed. The deadline
        # bounds when the worker sends and stops waiting, not when the
        # network delivers: the key safety margin absorbs that delay.
        remaining = (
            profile.request_deadline.total_seconds()
            - (monotonic() - started)
        )
        if remaining <= 0:
            # Not sent. Recorded conservatively as unknown, never as
            # proof of non-dispatch.
            outcome = _Outcome(
                S.UNKNOWN, AttemptOutcome.MAY_HAVE_DISPATCHED, None, None,
                "request_deadline_elapsed_before_send", sent=False,
            )
        else:
            outcome = self._dispatch(connector, held, remaining)
        self._fault("after_provider_call", connection)

        # 5. Persist evidence and outcome together.
        return self._persist(connection, fence, outcome, result)

    def _release(self, connection, held, reason):
        try:
            lifecycle.release_claim(
                connection, self._schema, held, reason=reason
            )
            self._commit(connection, "release")
        except lifecycle.LifecycleRejected:
            connection.rollback()
        except _Uncertain:
            # Unknown whether the release landed; the lease-expiry exit
            # covers it either way.
            pass

    def _dispatch(self, connector, held, timeout):
        started = monotonic()
        try:
            sent = connector.dispatch(
                held.intent, operation_id=held.operation_id,
                timeout=timeout,
            )
        except (ConnectionError, OSError):
            sent = None
        elapsed = monotonic() - started
        kind = "ambiguous" if sent is None else sent.kind.value
        reason = "ambiguous_no_response" if sent is None else sent.reason
        connector_id = held.intent.connector_id
        metrics.DISPATCH_SECONDS.labels(connector_id).observe(elapsed)
        metrics.DISPATCH_OUTCOMES.labels(connector_id, kind, reason).inc()
        log.info("dispatch_outcome", extra={
            "operation_id": str(held.operation_id),
            "claim_id": str(held.claim_id), "connector": connector_id,
            "outcome": kind, "reason": reason,
            "latency_ms": round(elapsed * 1000, 1),
        })

        if sent is None or sent.kind is DispatchKind.AMBIGUOUS:
            return _Outcome(
                S.UNKNOWN, AttemptOutcome.MAY_HAVE_DISPATCHED,
                None if sent is None else sent.observed, None,
                "ambiguous_no_response" if sent is None else sent.reason,
                facts=() if sent is None else sent.provider_facts,
            )

        if sent.kind is DispatchKind.NOT_APPLIED and sent.retryable:
            # Proven not applied, but the contract allows another try:
            # the reconciler's policy decides, under the SQL retry guard.
            return _Outcome(
                S.UNKNOWN, AttemptOutcome.PROVEN_NOT_APPLIED,
                EvidenceKind.PROVEN_NOT_APPLIED, sent.reference,
                RETRY_PENDING_REASON, facts=sent.provider_facts,
            )

        if sent.kind is DispatchKind.NOT_APPLIED:
            return _Outcome(
                S.FAILED_CONFIRMED, AttemptOutcome.PROVEN_NOT_APPLIED,
                EvidenceKind.PROVEN_NOT_APPLIED, sent.reference,
                sent.reason, facts=sent.provider_facts,
            )

        return _Outcome(
            S.SUCCEEDED, AttemptOutcome.CONFIRMED_APPLIED,
            EvidenceKind.APPLIED, sent.reference, sent.reason,
            facts=sent.provider_facts,
        )

    def _write_outcome(self, connection, fence, outcome):
        lifecycle.record_attempt_outcome(
            connection, self._schema, fence.attempt_id,
            outcome.attempt_outcome,
            evidence_kind=outcome.evidence_kind,
            reference=outcome.reference,
            details=outcome.details(),
        )
        lifecycle.record_dispatch_outcome(
            connection, self._schema, fence, outcome.state,
            reason=outcome.reason,
        )

    def _write_late_evidence(self, connection, fence, outcome):
        lifecycle.record_attempt_outcome(
            connection, self._schema, fence.attempt_id,
            outcome.attempt_outcome,
            evidence_kind=outcome.evidence_kind,
            reference=outcome.reference,
            details=outcome.details(late=True, reason=outcome.reason),
        )

    def _persist(self, connection, fence, outcome, result):
        try:
            self._write_outcome(connection, fence, outcome)
            self._commit(connection, "outcome")
            return result(_FROM_STATE[outcome.state], "outcome", outcome.sent)
        except lifecycle.StaleTransition:
            # A newer decision exists (for example recovery after the
            # lease expired). Record this reply as evidence only.
            connection.rollback()
            return self._late(connection, fence, outcome, result)
        except _Uncertain:
            pass

        # The outcome commit is uncertain. Observe by identity before
        # repeating the write; the version check makes one repeat safe.
        try:
            observer = self._connect()
        except UNCERTAIN_ERRORS:
            return result(ExecutionStatus.UNCERTAIN, "outcome", outcome.sent)

        try:
            current = lifecycle.current_state(
                observer, self._schema, fence.operation_id
            )
            observer.commit()

            if current is None:
                return result(
                    ExecutionStatus.UNCERTAIN, "outcome", outcome.sent,
                )

            landed = (
                current.seq == fence.seq + 1
                and current.state is outcome.state
                and current.actor == fence_actor(observer)
            )
            if landed:
                return result(
                    _FROM_STATE[outcome.state], "observed", outcome.sent,
                )

            if (
                current.seq != fence.seq
                or current.state is not S.DISPATCHING
            ):
                return self._late(observer, fence, outcome, result)

            self._write_outcome(observer, fence, outcome)
            self._commit(observer, "outcome_retry")
            return result(
                _FROM_STATE[outcome.state], "outcome_retry", outcome.sent,
            )
        except lifecycle.StaleTransition:
            observer.rollback()
            return self._late(observer, fence, outcome, result)
        except (_Uncertain, *UNCERTAIN_ERRORS):
            return result(ExecutionStatus.UNCERTAIN, "outcome", outcome.sent)
        finally:
            observer.close()

    def _late(self, connection, fence, outcome, result):
        try:
            self._write_late_evidence(connection, fence, outcome)
            self._commit(connection, "late_evidence")
        except (_Uncertain, *UNCERTAIN_ERRORS):
            return result(
                ExecutionStatus.UNCERTAIN, "late_evidence", outcome.sent,
            )
        return result(
            ExecutionStatus.LATE_EVIDENCE, "late_evidence", outcome.sent,
        )
