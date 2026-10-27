"""Reconciler for crashed and commit-uncertain operations.

Experimental and disposable-schema only. It never dispatches: its only
connector call is ``lookup``. An expired lease triggers investigation,
never a second dispatch. Every decision is bound to the operation's
registered connector contract, read from the database.

Per operation, one pass moves at most:
  DISPATCHING (lease expired) -> UNKNOWN -> VERIFYING -> decision
or, for an escalated operation, a bounded lookup-only check that may
reopen it:
  ESCALATED_UNRESOLVED -> VERIFYING -> decision
where the decision comes from the pure ``core.recovery.decide_recovery``
policy over the provider lookup and the recorded attempt evidence:

  CONFIRM_APPLIED                  -> SUCCEEDED
  CONFIRM_NOT_APPLIED              -> FAILED_CONFIRMED
  RETRY_AFTER_PROOF/RETRY_SAME_KEY -> RETRY_ELIGIBLE (reclaimed by a
                                      worker; the SQL retry guard decides)
  VERIFY_LATER                     -> UNKNOWN (only before the SQL
                                      investigation deadline)
  ESCALATE_UNRESOLVED/REJECT       -> ESCALATED_UNRESOLVED

The database independently enforces legality, leases, and that
SUCCEEDED / FAILED_CONFIRMED have matching recorded evidence.

A CLAIMED operation whose claim lease expired leaves CLAIMED: to
RETRY_ELIGIBLE if no attempt was ever made, otherwise to UNKNOWN for
re-verification. Nothing is dispatched while CLAIMED.
"""

import logging

from collections import Counter
from contextlib import closing
from time import monotonic
from dataclasses import dataclass, field
from enum import Enum


from core.attempts import (
    AttemptInventory,
    AttemptObservation,
    AttemptOutcome,
)
from connectors.base import LookupResult, LookupTarget, completed_lookup
from core.contracts import (
    Evidence,
    EvidenceKind,
    LookupSemantics,
    OperationIdentity,
)
from core.lifecycle import (
    APPROVAL_EXPIRED_REASON,
    COMPLETED_STATES,
    RETRY_PENDING_REASON,
    UNRESOLVED_STATES,
    LifecycleState as S,
)
from core.recovery import DecisionKind, RecoveryRequest, decide_recovery
from observability import metrics
from storage import durable_lifecycle as lifecycle
from storage.experimental_durable_execution import (
    UNCERTAIN_ERRORS,
    connector_map,
)


log = logging.getLogger("safeact.reconciler")

_DECISION_STATE = {
    DecisionKind.CONFIRM_APPLIED: S.SUCCEEDED,
    DecisionKind.CONFIRM_NOT_APPLIED: S.FAILED_CONFIRMED,
    DecisionKind.RETRY_AFTER_PROOF: S.RETRY_ELIGIBLE,
    DecisionKind.RETRY_SAME_KEY: S.RETRY_ELIGIBLE,
    DecisionKind.VERIFY_LATER: S.UNKNOWN,
    DecisionKind.ESCALATE_UNRESOLVED: S.ESCALATED_UNRESOLVED,
    DecisionKind.REJECT_CONFLICT: S.ESCALATED_UNRESOLVED,
}

_LOOKUP_OUTCOME = {
    EvidenceKind.APPLIED: AttemptOutcome.CONFIRMED_APPLIED,
    EvidenceKind.PROVEN_NOT_APPLIED: AttemptOutcome.PROVEN_NOT_APPLIED,
    EvidenceKind.NOT_FOUND: AttemptOutcome.MAY_HAVE_DISPATCHED,
    EvidenceKind.INCONCLUSIVE: AttemptOutcome.MAY_HAVE_DISPATCHED,
}


class ReconcileStatus(str, Enum):
    RESOLVED = "resolved"
    WAITING = "waiting"
    UNCERTAIN = "uncertain"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    operation_id: object
    status: ReconcileStatus
    state: S
    decision: DecisionKind | None


class Reconciler:

    def __init__(
        self, *, connect, schema, connectors,
        retry_authorized=False, fault_hook=None,
        connection_scope=None,
    ):
        self._connect = connect
        self._connection_scope = connection_scope or (lambda: closing(connect()))
        self._schema = schema
        self._connectors = connector_map(connectors)
        # Policy authorization to retry at all; the contract's budget
        # and the SQL retry guard still apply.
        self._retry_authorized = retry_authorized
        self._fault = fault_hook or (lambda point, connection: None)

    # -- scanning ------------------------------------------------------

    def reconcile_once(self, *, limit=100):
        with self._connection_scope() as connection:
            candidates = []
            for state in (
                S.CLAIMED, S.DISPATCHING, S.UNKNOWN, S.VERIFYING,
                S.ESCALATED_UNRESOLVED, S.RETRY_ELIGIBLE,
            ):
                candidates += [
                    operation_id for operation_id, _ in
                    lifecycle.operations_in_state(
                        connection, self._schema, state, limit=limit,
                    )
                ]
            connection.commit()

            return [
                self.reconcile(operation_id, connection)
                for operation_id in candidates
            ]

    # -- one operation ---------------------------------------------------

    def reconcile(self, operation_id, connection):
        try:
            return self._reconcile(operation_id, connection)
        except _Uncertain as uncertain:
            return ReconcileResult(
                operation_id, ReconcileStatus.UNCERTAIN,
                uncertain.state, None,
            )

    def _commit(self, connection, stage, state):
        try:
            self._fault(f"before_{stage}_commit", connection)
            connection.commit()
            self._fault(f"after_{stage}_commit", connection)
        except UNCERTAIN_ERRORS as error:
            raise _Uncertain(state) from error

    def _move(self, connection, snap, to_state, reason, evidence=None):
        try:
            seq = lifecycle.recovery_transition(
                connection, self._schema, snap.operation_id,
                expected_seq=snap.seq, expected_state=snap.state,
                to_state=to_state, reason=reason, evidence=evidence,
            )
        except (
            lifecycle.StaleTransition,
            # The deadline passed between reading and moving; the next
            # pass escalates instead of verifying later.
            lifecycle.DeadlinePassed,
            # Reopen refused: no new decisive evidence, or budget spent.
            lifecycle.EvidenceRequired,
            lifecycle.ReopenRefused,
            # A reviewed contract's retry raced its approval: wait.
            lifecycle.ApprovalRequired,
            # 0018: the next paced lookup is not due yet.
            lifecycle.NotYetDue,
        ):
            connection.rollback()
            return None
        except (
            lifecycle.RetryNotProtected,
            lifecycle.RetryBudgetExhausted,
        ):
            connection.rollback()
            return _RETRY_REFUSED
        self._commit(connection, f"to_{to_state.value.lower()}", snap.state)
        decision = (evidence or {}).get("decision") or "move"
        metrics.DECISIONS.labels(decision, to_state.value).inc()
        log.info("reconciler_move", extra={
            "operation_id": str(snap.operation_id),
            "from_state": snap.state.value, "to_state": to_state.value,
            "reason": reason, "decision": decision, "seq": seq,
        })
        return seq

    def _snapshot(self, connection, operation_id):
        snap = lifecycle.recovery_snapshot(
            connection, self._schema, operation_id
        )
        connection.commit()
        return snap

    def _result(self, snap, status, decision=None):
        return ReconcileResult(
            snap.operation_id, status, snap.state, decision
        )

    def _profile(self, connection, snap):
        profile = lifecycle.connector_contract(
            connection, self._schema,
            snap.connector_id, snap.contract_version,
        )
        connection.commit()
        return profile

    def _status(self, connection, operation_id):
        status = lifecycle.investigation_status(
            connection, self._schema, operation_id
        )
        connection.commit()
        return status

    def _reconcile(self, operation_id, connection):
        snap = self._snapshot(connection, operation_id)

        if snap.state is S.RETRY_ELIGIBLE:
            # Reclaim blocked for good (abandoned-claim limit or attempt
            # budget): hand it to a human instead of parking it.
            blocked = lifecycle.reclaim_blocked(
                connection, self._schema, operation_id
            )
            connection.commit()
            if blocked is None:
                return self._result(snap, ReconcileStatus.SKIPPED)
            if self._move(
                connection, snap, S.ESCALATED_UNRESOLVED, blocked
            ) is None:
                return self._result(snap, ReconcileStatus.SKIPPED)
            return ReconcileResult(
                operation_id, ReconcileStatus.RESOLVED,
                S.ESCALATED_UNRESOLVED, None,
            )

        if snap.state is S.ESCALATED_UNRESOLVED:
            reopened = self._after_escalation(connection, snap)
            if isinstance(reopened, ReconcileResult):
                return reopened
            snap = reopened

        if snap.state is S.CLAIMED:
            # Abandoned claim: nothing was dispatched under it. With no
            # attempt ever made it may be claimed again; otherwise it
            # is verified again. The database enforces the same rule.
            if snap.lease_expires_at > snap.observed_at:
                return self._result(snap, ReconcileStatus.WAITING)
            target = S.UNKNOWN if snap.attempts else S.RETRY_ELIGIBLE
            if self._move(
                connection, snap, target, "claim_abandoned_lease_expired"
            ) is None:
                return self._result(snap, ReconcileStatus.SKIPPED)
            if target is S.RETRY_ELIGIBLE:
                return ReconcileResult(
                    operation_id, ReconcileStatus.RESOLVED, target, None
                )
            snap = self._snapshot(connection, operation_id)

        if snap.state is S.DISPATCHING:
            if snap.lease_expires_at > snap.observed_at:
                return self._result(snap, ReconcileStatus.WAITING)
            if self._move(
                connection, snap, S.UNKNOWN, "lease_expired_investigate"
            ) is None:
                return self._result(snap, ReconcileStatus.SKIPPED)
            snap = self._snapshot(connection, operation_id)

        if snap.state is S.UNKNOWN:
            # 0018: one lookup per paced slot, not one per pass (F16). The
            # database refuses an early move too.
            pacing = lifecycle.pacing(connection, self._schema, operation_id)
            connection.commit()
            if not pacing.lookup_due():
                return self._result(snap, ReconcileStatus.WAITING)
            if self._move(
                connection, snap, S.VERIFYING, "verify_with_provider"
            ) is None:
                return self._result(snap, ReconcileStatus.SKIPPED)
            snap = self._snapshot(connection, operation_id)

        if snap.state is not S.VERIFYING:
            return self._result(snap, ReconcileStatus.SKIPPED)

        # A contract whose review policy requires it retries only on a
        # reviewer's approval, which performs the move itself. Until the
        # investigation deadline the operation waits, without repeated
        # lookups (each would change the evidence the reviewer sees);
        # after it, it is decided without a retry. Elsewhere the
        # reconciler's own retry authorization applies.
        approval = lifecycle.retry_approval_status(
            connection, self._schema, operation_id
        )
        connection.commit()
        approval_expired = False
        if approval == "awaiting":
            status = self._status(connection, operation_id)
            if (
                status.investigation_deadline is not None
                and status.observed_at < status.investigation_deadline
            ):
                return self._result(snap, ReconcileStatus.WAITING)
            approval_expired = True
        may_retry = approval == "not_required"

        profile = self._profile(connection, snap)
        evidence = self._lookup(connection, snap, profile)
        snap = self._snapshot(connection, operation_id)
        status = self._status(connection, operation_id)
        decision = decide_recovery(
            self._request(snap, evidence, profile, status, retry=may_retry)
        )
        target = _DECISION_STATE[decision.kind]
        reason = decision.reason
        if approval_expired and target is S.FAILED_CONFIRMED:
            # Not done because no reviewer approved the retry in time:
            # reported apart from a permanent provider rejection. The
            # database accepts this reason only with system proof.
            reason = APPROVAL_EXPIRED_REASON

        moved = self._move(
            connection, snap, target, reason,
            {"decision": decision.kind.value,
             "lookup": evidence.kind.value,
             "reference": evidence.reference},
        )
        if moved is _RETRY_REFUSED:
            # The database found the retry unprotected or over budget
            # (for example, key retention lapsed). Decide again without
            # a retry; the SQL guard is the authority.
            decision = decide_recovery(self._request(
                snap, evidence, profile, status, retry=False,
            ))
            target = _DECISION_STATE[decision.kind]
            moved = self._move(
                connection, snap, target, decision.reason,
                {"decision": decision.kind.value,
                 "lookup": evidence.kind.value,
                 "reference": evidence.reference,
                 "retry_refused": True},
            )
        if moved is None or moved is _RETRY_REFUSED:
            return self._result(snap, ReconcileStatus.SKIPPED, decision.kind)

        status = (
            ReconcileStatus.WAITING if target is S.UNKNOWN
            else ReconcileStatus.RESOLVED
        )
        return ReconcileResult(operation_id, status, target, decision.kind)

    def _after_escalation(self, connection, snap):
        """Bounded, lookup-only collection of evidence after escalation.

        At most one lookup per check interval, none after the horizon,
        none for a contract without lookup. A decisive answer reopens
        verification; the database decides whether the reopen is
        justified (new row, observed at this escalation, within budget).
        """

        # Evidence already recorded after this escalation (for example
        # a late worker reply) reopens without any provider call, so
        # the lookup interval and horizon do not apply. The database
        # still enforces one row per reopen and max_reopens.
        if lifecycle.reopen_evidence(
            connection, self._schema, snap.operation_id
        ):
            connection.commit()
            if self._move(
                connection, snap, S.VERIFYING, "reopen_recorded_evidence",
            ) is None:
                return self._result(snap, ReconcileStatus.SKIPPED)
            return self._snapshot(connection, snap.operation_id)
        connection.commit()

        profile = self._profile(connection, snap)
        status = self._status(connection, snap.operation_id)

        if (
            profile.contract.lookup_semantics
            is LookupSemantics.UNAVAILABLE
            or not snap.attempts
            or status.escalated_at is None
        ):
            return self._result(snap, ReconcileStatus.SKIPPED)

        source = "post_escalation"
        beyond_horizon = (
            status.observed_at
            >= status.escalated_at + profile.post_escalation_horizon
        )
        too_soon = (
            status.last_lookup_at is not None
            and status.observed_at
            < status.last_lookup_at + profile.post_escalation_check_interval
        )
        if not (beyond_horizon or too_soon):
            # Inside the bounds the lookup runs anyway; an investigator's
            # queued request is consumed by it and recorded as its cause.
            if lifecycle.take_verification_request(
                connection, self._schema, snap.operation_id
            ) is not None:
                source = "requested_verification"
            self._commit(connection, "verification_request_taken", snap.state)
        if beyond_horizon or too_soon:
            # Outside the contract's bounds only on a reviewer-approved
            # REVERIFY, consumed here: exactly one extra lookup.
            granted = lifecycle.take_reverify(
                connection, self._schema, snap.operation_id
            )
            self._commit(connection, "reverify_taken", snap.state)
            if granted is None:
                return self._result(
                    snap,
                    ReconcileStatus.SKIPPED if beyond_horizon
                    else ReconcileStatus.WAITING,
                )
            source = "reviewer_reverify"

        found = self._observe(connection, snap, profile, source)
        if found.kind not in (
            EvidenceKind.APPLIED, EvidenceKind.PROVEN_NOT_APPLIED,
        ):
            return self._result(snap, ReconcileStatus.WAITING)

        snap = self._snapshot(connection, snap.operation_id)
        if self._move(
            connection, snap, S.VERIFYING, "reopen_new_evidence",
            {"lookup": found.kind.value, "reference": found.reference},
        ) is None:
            return self._result(snap, ReconcileStatus.SKIPPED)
        return self._snapshot(connection, snap.operation_id)

    def _observe(self, connection, snap, profile, source):
        """Ask the connector and record its answer as attempt evidence."""

        connector = self._connectors.get(snap.connector_id)

        started = monotonic()
        if connector is None:
            found = LookupResult(
                EvidenceKind.INCONCLUSIVE, note="no_connector"
            )
        else:
            target = LookupTarget(
                operation_id=snap.operation_id,
                provider_account_id=snap.provider_account_id,
                provider_idempotency_key=snap.provider_idempotency_key,
                payload_sha256=snap.payload_sha256,
                first_dispatch_at=snap.first_dispatch_at,
                observed_at=snap.observed_at,
            )
            found = connector.lookup(target, profile)
            # Native negative proof is checked again against DB time
            # once the lookup has finished (it may cross retention).
            if (
                found.kind is EvidenceKind.PROVEN_NOT_APPLIED
                and profile.contract.native_idempotency is not None
            ):
                finished_at = self._snapshot(
                    connection, snap.operation_id
                ).observed_at
                found = completed_lookup(profile, target, found, finished_at)
        elapsed = monotonic() - started
        metrics.LOOKUP_SECONDS.labels(snap.connector_id).observe(elapsed)
        metrics.LOOKUPS.labels(snap.connector_id, found.kind.value).inc()
        log.info("lookup", extra={
            "operation_id": str(snap.operation_id),
            "connector": snap.connector_id, "evidence": found.kind.value,
            "note": found.note, "pass": source,
            "latency_ms": round(elapsed * 1000, 1),
        })

        if snap.attempts:
            details = {"source": "reconciler_lookup", "pass": source}
            if found.note:
                details["note"] = found.note
            if found.provider_facts:
                # Recorded only; the decision below never reads it.
                details["provider"] = dict(found.provider_facts)
            lifecycle.record_attempt_outcome(
                connection, self._schema, snap.attempts[-1].attempt_id,
                _LOOKUP_OUTCOME[found.kind],
                evidence_kind=found.kind,
                reference=found.reference,
                details=details,
            )
            self._commit(connection, "lookup_evidence", snap.state)
        return found

    def _lookup(self, connection, snap, profile):
        found = self._observe(connection, snap, profile, "verification")
        kind, reference = found.kind, found.reference

        # A recorded provider receipt (for example a late worker
        # reply) is authoritative even if the lookup lags behind.
        receipts = [
            a.applied_reference for a in snap.attempts
            if a.applied_reference
        ]
        if kind is not EvidenceKind.APPLIED and receipts:
            kind, reference = EvidenceKind.APPLIED, receipts[-1]

        # Every attempt carries recorded proof of non-application (for
        # example a definitive pre-execution rejection): that proof
        # stands even when an eventual lookup cannot see anything.
        proofs = [a.not_applied_reference for a in snap.attempts]
        if (
            kind not in (
                EvidenceKind.APPLIED, EvidenceKind.PROVEN_NOT_APPLIED,
            )
            and proofs and all(proofs)
        ):
            kind, reference = EvidenceKind.PROVEN_NOT_APPLIED, proofs[-1]

        return Evidence(
            kind=kind,
            connector_id=snap.connector_id,
            operation_id=str(snap.operation_id),
            provider_account_id=snap.provider_account_id,
            contract_version=snap.contract_version,
            reference=reference,
        )

    def _request(self, snap, evidence, profile, status, retry=True):
        identity = OperationIdentity(
            operation_id=str(snap.operation_id),
            tenant_id=snap.tenant_id,
            action_type=snap.action_type,
            resource_id=snap.resource_id,
            provider_account_id=snap.provider_account_id,
            connector_id=snap.connector_id,
            payload_sha256=snap.payload_sha256,
        )

        observations = []
        for attempt in snap.attempts:
            if attempt.applied_reference:
                outcome, proof = (
                    AttemptOutcome.CONFIRMED_APPLIED,
                    attempt.applied_reference,
                )
            elif attempt.not_applied_reference:
                outcome, proof = (
                    AttemptOutcome.PROVEN_NOT_APPLIED,
                    attempt.not_applied_reference,
                )
            else:
                outcome, proof = AttemptOutcome.MAY_HAVE_DISPATCHED, None

            observations.append(AttemptObservation(
                attempt_id=str(attempt.attempt_id),
                operation_id=str(snap.operation_id),
                connector_id=snap.connector_id,
                provider_account_id=snap.provider_account_id,
                outcome=outcome,
                proof_reference=proof,
            ))

        first = snap.first_dispatch_at
        # The database's deadline; None (no dispatch or no contract)
        # counts as already passed.
        deadline = status.investigation_deadline or first
        return RecoveryRequest(
            original=identity,
            candidate=identity,
            original_provider_key=snap.provider_idempotency_key,
            candidate_provider_key=snap.provider_idempotency_key,
            contract=profile.contract,
            pinned_contract_version=snap.contract_version,
            evidence=evidence,
            first_dispatch_at=first,
            investigation_deadline=max(deadline, first),
            now=max(status.observed_at, first),
            # The contract's attempt budget, counted from durable
            # attempts; the database enforces the same budget.
            remaining_retries=max(
                0, profile.max_attempts - len(snap.attempts)
            ) if retry else 0,
            retry_authorized=self._retry_authorized and retry,
            attempt_inventory=AttemptInventory(
                operation_id=str(snap.operation_id),
                connector_id=snap.connector_id,
                provider_account_id=snap.provider_account_id,
                contract_version=snap.contract_version,
                is_complete=True,
                attempts=tuple(observations),
            ),
        )


# _move result when the SQL retry guard refuses RETRY_ELIGIBLE.
_RETRY_REFUSED = object()


class _Uncertain(Exception):
    def __init__(self, state):
        super().__init__(state)
        self.state = state


def account_operations(connection, schema):
    """Count every admitted operation by current state (A5).

    UNKNOWN and VERIFYING work is also split by why the dispatch outcome
    was unknown, so a definitive rejection awaiting a retry decision is
    never reported as an ambiguous outcome.
    """

    rows = lifecycle.accounting_rows(connection, schema)
    provenance = lifecycle.succeeded_by_provenance(connection, schema)
    final = lifecycle.outcome_reasons(connection, schema)
    connection.commit()

    by_state = Counter()
    by_reason = Counter()
    for state, reason, count in rows:
        by_state[state] += count
        if reason is not None:
            by_reason[reason] += count
    return Accounting(by_state, by_reason, provenance, final)


@dataclass(frozen=True)
class Accounting:
    by_state: Counter
    unknown_by_reason: Counter
    # SUCCEEDED split into provider_verified and human_attested.
    succeeded_by_provenance: dict = field(default_factory=dict)
    # Finished work by (state, final reason).
    final_reasons: dict = field(default_factory=dict)

    @property
    def total(self):
        return sum(self.by_state.values())

    @property
    def completed(self):
        return sum(self.by_state[s] for s in COMPLETED_STATES)

    @property
    def unresolved(self):
        return sum(self.by_state[s] for s in UNRESOLVED_STATES)

    @property
    def retry_eligible(self):
        return self.by_state[S.RETRY_ELIGIBLE]

    @property
    def succeeded_provider_verified(self):
        return self.succeeded_by_provenance.get("provider_verified", 0)

    @property
    def succeeded_human_attested(self):
        """Successes that rest on a reviewer's attestation alone."""
        return self.succeeded_by_provenance.get("human_attested", 0)

    @property
    def failed_retry_approval_expired(self):
        """Confirmed not done only because no reviewer approved a retry."""
        return self.final_reasons.get(
            (S.FAILED_CONFIRMED.value, APPROVAL_EXPIRED_REASON), 0
        )

    @property
    def cancelled_before_claim(self):
        """Cancelled on request before any claim: never dispatched."""
        return self.by_state[S.CANCELLED_BEFORE_CLAIM]

    @property
    def retry_pending(self):
        """UNKNOWN/VERIFYING after a definitive, retryable rejection."""
        return self.unknown_by_reason[RETRY_PENDING_REASON]

    @property
    def in_flight(self):
        return sum(
            self.by_state[s] for s in (
                S.RECORDED, S.CLAIMED, S.DISPATCHING,
                S.UNKNOWN, S.VERIFYING, S.RETRY_ELIGIBLE,
            )
        )
