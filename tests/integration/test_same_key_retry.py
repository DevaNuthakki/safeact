"""Same-key retry from RETRY_ELIGIBLE (0007): F02, F06, F09, A3 in SQL.

A retry is sent only while the database finds it protected: proof of
non-application for every earlier attempt, or a native key inside its
retention cutoff, rechecked at the fence. Effects are counted from the
mock providers' harness ledger. Time-dependent assertions are clearly
before or clearly after each boundary, on the database clock.
"""

import os
import threading
import time
import unittest

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql

from connectors.crm import CrmConnector
from connectors.crm_mock import MockCrmProvider
from connectors.email import EmailConnector
from connectors.email_mock import MockEmailProvider
from connectors.ledger import EffectsLedger
from connectors.payments import PaymentsConnector
from connectors.payments_mock import MockPaymentsProvider
from core.attempts import AttemptOutcome
from core.contracts import EvidenceKind
from core.lifecycle import RETRY_PENDING_REASON, LifecycleState as S
from core.recovery import DecisionKind as D
from storage import durable_lifecycle as L
from storage.experimental_durable_execution import (
    ExecutionStatus as X,
    ExecutionTask,
    ExecutionWorker,
    InMemoryTaskQueue,
    relay_once,
)
from storage.experimental_recovery_worker import (
    Reconciler,
    account_operations,
)
from tests.integration.lifecycle_fixture import (
    DisposableLifecycleDatabase,
    commit,
)
from tests.integration.profiles_fixture import (
    CRM_FAST,
    CRM_SLOW,
    EMAIL_SLOW,
    PAYMENTS_FAST,
    PAYMENTS_RETENTION,
    PAYMENTS_SLOW,
)
from tests.integration.timing import (
    assert_clearly_before,
    margin_for,
    wait_until_past,
)


LEASE = 1

PROFILES = {
    "payments": dict(
        connector_id="mock-payments", action_type="refund_payment",
        resource_id="synthetic-charge",
        payload={"charge_id": "synthetic-charge", "amount_minor": 1200},
    ),
    "crm": dict(
        connector_id="mock-crm", action_type="create_replacement_order",
        resource_id="synthetic-case",
        payload={"case_id": "synthetic-case", "sku": "sku-1"},
        provider_idempotency_key=None,
    ),
    "email": dict(
        connector_id="mock-email", action_type="send_refund_confirmation",
        resource_id="synthetic-customer",
        payload={"to": "customer@example.invalid", "template": "refund"},
        provider_idempotency_key=None,
    ),
}


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class SameKeyRetryTests(unittest.TestCase):

    def setUp(self):
        self.db = DisposableLifecycleDatabase()
        self.addCleanup(self.db.cleanup)
        self.db.start()
        self.schema = self.db.schema
        self.clock = self.db.connect(self.db.recoverer, autocommit=True)

        self.ledger = EffectsLedger()
        self.payments = MockPaymentsProvider(ledger=self.ledger)
        self.crm = MockCrmProvider(ledger=self.ledger)
        self.email = MockEmailProvider(ledger=self.ledger)
        self.admitted = {}
        self.addCleanup(self.assert_invariants)

    # -- helpers -------------------------------------------------------

    def connectors(self):
        return [
            PaymentsConnector(self.payments),
            CrmConnector(self.crm),
            EmailConnector(self.email),
        ]

    def admit(self, profile, contract, **changes):
        n = len(self.admitted) + 1
        fields = {**PROFILES[profile], **changes}
        operation_id = self.db.admit(
            request_key=f"{profile}-{n}",
            contract_version=contract.version, **fields,
        )
        # Ledger correlation: the derived provider key, or the
        # operation id for connectors without one.
        self.admitted[operation_id] = (
            self.db.provider_key(operation_id) or str(operation_id)
        )
        return operation_id

    def worker(self, hook=None):
        return ExecutionWorker(
            connect=lambda: self.db.connect(self.db.worker_a),
            schema=self.schema, connectors=self.connectors(),
            lease_seconds=LEASE, fault_hook=hook,
        )

    def execute(self, operation_id, hook=None):
        return self.worker(hook).execute(
            ExecutionTask(operation_id, uuid4(), uuid4())
        )

    def relay_and_run(self):
        """Publish claimable work (including retries) and run it."""

        queue = InMemoryTaskQueue()
        relay = self.db.connect(self.db.worker_b)
        relay_once(relay, self.schema, queue)
        return self.worker().run_until_idle(queue)

    def reconcile(self, retry_authorized=True):
        return Reconciler(
            connect=lambda: self.db.connect(self.db.recoverer),
            schema=self.schema, connectors=self.connectors(),
            retry_authorized=retry_authorized,
        ).reconcile_once()

    def current(self, operation_id):
        return L.current_state(self.clock, self.schema, operation_id)

    def state(self, operation_id):
        return self.current(operation_id).state

    def first_fence(self, operation_id):
        return self.db.admin.execute(sql.SQL(
            "SELECT min(created_at) FROM {} WHERE operation_id = %s "
            "AND to_state = 'DISPATCHING'"
        ).format(self.db.table("safeact_operation_transitions")),
            (operation_id,),
        ).fetchone()[0]

    def unknown_reasons(self, operation_id):
        return [reason for (reason,) in self.db.admin.execute(sql.SQL(
            "SELECT reason FROM {} WHERE operation_id = %s "
            "AND from_state = 'DISPATCHING' AND to_state = 'UNKNOWN' "
            "ORDER BY seq"
        ).format(self.db.table("safeact_operation_transitions")),
            (operation_id,),
        ).fetchall()]

    def attempts(self, operation_id):
        return self.db.count("safeact_attempts", operation_id=operation_id)

    def calls(self, operation_id):
        return len(self.ledger.calls_for(self.admitted[operation_id]))

    def effects(self, operation_id):
        return len(self.ledger.effects_for(self.admitted[operation_id]))

    def wait_lease(self, operation_id):
        lease = self.current(operation_id).lease_expires_at
        if lease is not None:
            wait_until_past(self.clock, lease, margin_for(LEASE))

    def accounting(self):
        return account_operations(
            self.db.connect(self.db.recoverer), self.schema
        )

    def assert_invariants(self):
        accounting = self.accounting()
        self.assertEqual(accounting.total, len(self.admitted))
        for operation_id in self.admitted:
            self.assertLessEqual(self.effects(operation_id), 1)

    # -- F02: rejected before execution ---------------------------------

    def test_f02_payments_retries_once_after_proof(self):
        operation_id = self.admit("payments", PAYMENTS_FAST)
        self.payments.reject_next_retryable = "rej_busy_1"

        self.assertEqual(self.execute(operation_id).status, X.UNKNOWN)
        self.assertEqual(
            self.unknown_reasons(operation_id), [RETRY_PENDING_REASON]
        )
        accounting = self.accounting()
        self.assertEqual(accounting.retry_pending, 1)
        self.assertEqual(accounting.unresolved, 0)

        (decided,) = self.reconcile()
        self.assertEqual(decided.decision, D.RETRY_AFTER_PROOF)
        self.assertIs(self.state(operation_id), S.RETRY_ELIGIBLE)

        (retried,) = self.relay_and_run()
        self.assertEqual(retried.status, X.SUCCEEDED)
        self.assertEqual(self.attempts(operation_id), 2)
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (2, 1)
        )

    def test_f02_crm_retries_once_on_recorded_proof_despite_eventual_search(self):
        operation_id = self.admit("crm", CRM_FAST)
        self.crm.reject_next_retryable = "rej_locked_1"
        self.assertEqual(self.execute(operation_id).status, X.UNKNOWN)

        # Search is eventual and sees nothing; the recorded definitive
        # rejection is the proof.
        (decided,) = self.reconcile()
        self.assertEqual(decided.decision, D.RETRY_AFTER_PROOF)

        (retried,) = self.relay_and_run()
        self.assertEqual(retried.status, X.SUCCEEDED)
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (2, 1)
        )

    def test_f02_without_retry_authorization_failure_is_confirmed(self):
        operation_id = self.admit("crm", CRM_FAST)
        self.crm.reject_next_retryable = "rej_locked_2"
        self.execute(operation_id)

        (decided,) = self.reconcile(retry_authorized=False)
        self.assertEqual(decided.decision, D.CONFIRM_NOT_APPLIED)
        self.assertIs(self.state(operation_id), S.FAILED_CONFIRMED)
        self.assertEqual(self.relay_and_run(), [])
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (1, 0)
        )

    def test_f02_permanent_rejection_is_never_retried(self):
        operation_id = self.admit("payments", PAYMENTS_FAST)
        self.payments.reject_next = "rej_permanent"
        self.assertEqual(
            self.execute(operation_id).status, X.FAILED_CONFIRMED
        )
        self.assertEqual(self.reconcile(), [])
        self.assertEqual(self.relay_and_run(), [])
        self.assertEqual(self.calls(operation_id), 1)

    def test_attempt_budget_ends_in_confirmed_failure(self):
        # CRM_FAST allows 2 attempts.
        operation_id = self.admit("crm", CRM_FAST)
        self.crm.reject_next_retryable = "rej_1"
        self.execute(operation_id)
        self.reconcile()
        self.crm.reject_next_retryable = "rej_2"
        (second,) = self.relay_and_run()
        self.assertEqual(second.status, X.UNKNOWN)

        # The SQL guard refuses a third attempt even if asked directly.
        recoverer = self.db.connect(self.db.recoverer)
        seq = self.current(operation_id).seq
        seq = commit(recoverer, lambda: L.recovery_transition(
            recoverer, self.schema, operation_id, expected_seq=seq,
            expected_state=S.UNKNOWN, to_state=S.VERIFYING,
            reason="test",
        ))
        with self.assertRaises(L.RetryBudgetExhausted):
            commit(recoverer, lambda: L.recovery_transition(
                recoverer, self.schema, operation_id, expected_seq=seq,
                expected_state=S.VERIFYING, to_state=S.RETRY_ELIGIBLE,
                reason="test",
            ))

        (final,) = self.reconcile()
        self.assertEqual(final.decision, D.CONFIRM_NOT_APPLIED)
        self.assertIs(self.state(operation_id), S.FAILED_CONFIRMED)
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (2, 0)
        )

    # -- F06: lease expires while the original request runs -------------

    def run_held(self, operation_id, provider):
        """Start a worker whose request the provider holds open."""

        held = provider.hold_next()
        results = []
        thread = threading.Thread(
            target=lambda: results.append(self.execute(operation_id))
        )
        thread.start()
        deadline = time.monotonic() + 5
        while self.calls(operation_id) == 0:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        return held, thread, results

    def test_f06_payments_same_key_retry_is_absorbed_by_the_provider(self):
        operation_id = self.admit("payments", PAYMENTS_SLOW)
        held, original, results = self.run_held(operation_id, self.payments)
        self.wait_lease(operation_id)

        # In flight: the strong lookup cannot prove anything.
        (decided,) = self.reconcile()
        self.assertEqual(decided.decision, D.RETRY_SAME_KEY)

        (retry,) = self.relay_and_run()
        self.assertEqual(retry.status, X.UNKNOWN)
        self.assertEqual(
            self.unknown_reasons(operation_id)[-1],
            "provider_in_flight_conflict",
        )

        held.release()
        original.join(5)
        # The original's late reply is evidence, not an overwrite.
        self.assertEqual(results[0].status, X.LATE_EVIDENCE)

        self.reconcile()
        self.assertIs(self.state(operation_id), S.SUCCEEDED)
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (2, 1)
        )

    def test_f06_crm_lease_expiry_never_redispatches(self):
        operation_id = self.admit("crm", CRM_SLOW)
        held, original, results = self.run_held(operation_id, self.crm)
        self.wait_lease(operation_id)

        (decided,) = self.reconcile()
        self.assertEqual(decided.decision, D.VERIFY_LATER)
        self.assertEqual(self.relay_and_run(), [])

        held.release()
        original.join(5)
        self.assertEqual(results[0].status, X.LATE_EVIDENCE)
        (resolved,) = self.reconcile()
        self.assertEqual(resolved.decision, D.CONFIRM_APPLIED)
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (1, 1)
        )

    def test_f06_email_lease_expiry_escalates_and_never_resends(self):
        operation_id = self.admit("email", EMAIL_SLOW)
        held, original, results = self.run_held(operation_id, self.email)
        self.wait_lease(operation_id)

        (decided,) = self.reconcile()
        self.assertEqual(decided.decision, D.ESCALATE_UNRESOLVED)
        self.assertEqual(self.relay_and_run(), [])

        held.release()
        original.join(5)
        self.assertEqual(results[0].status, X.LATE_EVIDENCE)
        self.assertIs(self.state(operation_id), S.ESCALATED_UNRESOLVED)
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (1, 1)
        )

    # -- F09 and the request deadline -------------------------------------

    def retry_eligible_under_retention(self):
        """Response lost, effect hidden from the eventual lookup."""

        self.payments = MockPaymentsProvider(
            ledger=self.ledger,
            lookup_semantics=PAYMENTS_RETENTION.contract.lookup_semantics,
            retention_seconds=10,
        )
        operation_id = self.admit("payments", PAYMENTS_RETENTION)
        self.payments.hide_new_effects = True
        self.payments.fail_after_effect = True
        self.assertEqual(self.execute(operation_id).status, X.UNKNOWN)

        native = PAYMENTS_RETENTION.contract.native_idempotency
        cutoff = (
            self.first_fence(operation_id)
            + native.retention - native.safety_margin
        )
        margin = margin_for(native.retention - native.safety_margin)

        assert_clearly_before(self, self.clock, cutoff, margin)
        (decided,) = self.reconcile()
        self.assertEqual(decided.decision, D.RETRY_SAME_KEY)
        self.assertIs(self.state(operation_id), S.RETRY_ELIGIBLE)
        return operation_id, cutoff, margin

    def test_f09_retention_lapse_before_the_fence_blocks_the_replay(self):
        operation_id, cutoff, margin = self.retry_eligible_under_retention()

        # Clearly past the cutoff: the reclaim succeeds, the fence
        # refuses, and the claim is released for re-verification.
        wait_until_past(self.clock, cutoff, margin)
        (refused,) = self.relay_and_run()
        self.assertEqual((refused.status, refused.stage),
                         (X.NOT_CLAIMABLE, "fence"))
        self.assertIs(self.state(operation_id), S.UNKNOWN)
        self.assertEqual(self.attempts(operation_id), 1)

        # No longer protected: never retried, only verified.
        (later,) = self.reconcile()
        self.assertEqual(later.decision, D.VERIFY_LATER)
        self.assertEqual(self.relay_and_run(), [])
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (1, 1)
        )

        # The provider has forgotten the key by now: a naive replay
        # would be a second, unprotected refund.
        wait_until_past(
            self.clock,
            self.first_fence(operation_id)
            + PAYMENTS_RETENTION.contract.native_idempotency.retention,
            margin,
        )
        self.assertEqual(
            self.payments.lookup(self.admitted[operation_id]).status,
            "absent",
        )

    def test_fence_just_before_cutoff_sends_inside_retention(self):
        operation_id, cutoff, margin = self.retry_eligible_under_retention()

        assert_clearly_before(self, self.clock, cutoff, margin)
        (retried,) = self.relay_and_run()
        # The provider replays the original refund for the same key.
        self.assertEqual(retried.status, X.SUCCEEDED)
        # Provider-side receipt times (harness ledger): the retry
        # reached the provider while it still held the key.
        (_, first), (_, retry) = self.ledger.call_times()
        native = PAYMENTS_RETENTION.contract.native_idempotency
        self.assertLess(retry - first, native.retention.total_seconds())
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (2, 1)
        )

    def test_worker_past_its_request_deadline_never_sends(self):
        operation_id = self.admit("payments", PAYMENTS_FAST)
        deadline = PAYMENTS_FAST.request_deadline.total_seconds()

        def stall(point, _connection):
            if point == "after_fence_commit":
                time.sleep(
                    deadline + margin_for(PAYMENTS_FAST.request_deadline)
                    .total_seconds()
                )

        result = self.execute(operation_id, stall)
        self.assertEqual(result.status, X.UNKNOWN)
        self.assertFalse(result.provider_called)
        self.assertEqual(
            self.unknown_reasons(operation_id),
            ["request_deadline_elapsed_before_send"],
        )
        self.assertEqual(self.calls(operation_id), 0)

    # -- the SQL guard holds when the reconciler is bypassed -----------

    def drive_to_verifying(self, profile, contract):
        operation_id = self.admit(profile, contract)
        worker = self.db.connect(self.db.worker_a)
        recoverer = self.db.connect(self.db.recoverer)
        driven = self.db.drive_to(
            operation_id, S.VERIFYING, worker=worker, recoverer=recoverer,
        )
        return operation_id, driven, recoverer

    def test_sql_refuses_unprotected_retries_directly(self):
        cases = (
            ("email", EMAIL_SLOW),   # A3: no key, no lookup, no proof
            ("crm", CRM_FAST),       # no key, no proof
        )
        for profile, contract in cases:
            with self.subTest(profile=profile):
                operation_id, driven, recoverer = self.drive_to_verifying(
                    profile, contract
                )
                with self.assertRaises(L.RetryNotProtected):
                    self.db.recover(
                        recoverer, operation_id, driven.seq,
                        S.VERIFYING, S.RETRY_ELIGIBLE,
                    )

        # A known effect is never retried, even under a live key.
        operation_id, driven, recoverer = self.drive_to_verifying(
            "payments", PAYMENTS_FAST
        )
        self.db.prove(recoverer, driven.fence.attempt_id, applied=True)
        with self.assertRaises(L.RetryNotProtected):
            self.db.recover(
                recoverer, operation_id, driven.seq,
                S.VERIFYING, S.RETRY_ELIGIBLE,
            )

    def test_unknown_reason_vocabulary_is_enforced(self):
        operation_id = self.admit("payments", PAYMENTS_FAST)
        worker = self.db.connect(self.db.worker_a)
        driven = self.db.drive_to(
            operation_id, S.DISPATCHING, worker=worker,
            recoverer=self.db.connect(self.db.recoverer),
        )

        def outcome(reason):
            return commit(worker, lambda: L.record_dispatch_outcome(
                worker, self.schema, driven.fence, S.UNKNOWN, reason=reason,
            ))

        with self.assertRaises(psycopg.errors.InvalidParameterValue):
            outcome("timeout_probably")
        with self.assertRaises(L.EvidenceRequired):
            outcome(RETRY_PENDING_REASON)

        commit(worker, lambda: L.record_attempt_outcome(
            worker, self.schema, driven.fence.attempt_id,
            AttemptOutcome.PROVEN_NOT_APPLIED,
            evidence_kind=EvidenceKind.PROVEN_NOT_APPLIED,
            reference="rej_recorded",
        ))
        with self.assertRaises(L.EvidenceRequired):
            outcome("ambiguous_no_response")
        outcome(RETRY_PENDING_REASON)
        self.assertEqual(
            self.unknown_reasons(operation_id), [RETRY_PENDING_REASON]
        )

    # -- reclaim -------------------------------------------------------------

    def test_eight_concurrent_reclaims_have_one_owner(self):
        operation_id = self.admit("payments", PAYMENTS_FAST)
        driven = self.db.drive_to(
            operation_id, S.RETRY_ELIGIBLE,
            worker=self.db.connect(self.db.worker_a),
            recoverer=self.db.connect(self.db.recoverer),
        )
        contenders = [
            self.db.connect(role)
            for role in (self.db.worker_a, self.db.worker_b) * 4
        ]
        barrier = Barrier(len(contenders))

        def claim(connection):
            barrier.wait()
            try:
                commit(connection, lambda: L.claim(
                    connection, self.schema, operation_id, lease_seconds=60,
                ))
                return "won"
            except L.LifecycleRejected:
                return "lost"

        with ThreadPoolExecutor(len(contenders)) as pool:
            results = list(pool.map(claim, contenders))
        self.assertEqual(results.count("won"), 1)
        self.assertEqual(
            (self.state(operation_id), self.current(operation_id).seq),
            (S.CLAIMED, driven.seq + 1),
        )

    def test_unregistered_contract_is_released_then_never_reclaimed(self):
        # A contract version nobody registered: never dispatchable.
        operation_id = self.admit("payments", SimpleNamespace(version="v9"))

        first = self.execute(operation_id)
        self.assertEqual((first.status, first.stage),
                         (X.NOT_CLAIMABLE, "fence"))
        self.assertIs(self.state(operation_id), S.RETRY_ELIGIBLE)

        # No contract means no attempt budget: reclaim fails closed.
        again = self.execute(operation_id)
        self.assertEqual((again.status, again.stage),
                         (X.NOT_CLAIMABLE, "claim"))
        self.assertEqual(self.calls(operation_id), 0)

    def test_abandoned_claims_are_capped(self):
        operation_id = self.admit("payments", PAYMENTS_FAST)
        worker = self.db.connect(self.db.worker_a)

        def claim():
            return commit(worker, lambda: L.claim(
                worker, self.schema, operation_id, lease_seconds=60,
            ))

        for _ in range(3):
            held = claim()
            commit(worker, lambda: L.release_claim(
                worker, self.schema, held, reason="gave_up",
            ))

        with self.assertRaises(L.RetryBudgetExhausted):
            claim()
        self.assertEqual(commit(worker, lambda: L.claimable_operations(
            worker, self.schema, limit=100,
        )), ())
        # Still visible and counted, never dispatched.
        self.assertIs(self.state(operation_id), S.RETRY_ELIGIBLE)
        self.assertEqual(self.accounting().retry_eligible, 1)
        self.assertEqual(self.calls(operation_id), 0)

    def test_released_reclaim_with_prior_attempts_reverifies(self):
        operation_id = self.admit("payments", PAYMENTS_FAST)
        self.db.drive_to(
            operation_id, S.RETRY_ELIGIBLE,
            worker=self.db.connect(self.db.worker_a),
            recoverer=self.db.connect(self.db.recoverer),
        )
        worker = self.db.connect(self.db.worker_a)
        held = commit(worker, lambda: L.claim(
            worker, self.schema, operation_id, lease_seconds=60,
        ))
        released = commit(worker, lambda: L.release_claim(
            worker, self.schema, held, reason="gave_up",
        ))
        # An attempt exists, so the exit is verification, not retry.
        self.assertEqual(released[1], S.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
