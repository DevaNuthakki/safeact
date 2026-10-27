"""Crash and commit-uncertainty recovery for the mock refund slice.

Every scenario asserts, from the payments mock's own instrumentation,
at most one applied effect per operation and no redispatch; and, from
the database, that every admitted operation is still accounted for
and its transition history is contiguous and append-only.
"""

import os
import time
import unittest

from uuid import uuid4

from psycopg import sql

from core.contracts import LookupSemantics
from core.lifecycle import LifecycleState as S
from core.recovery import DecisionKind as D
from connectors.ledger import EffectsLedger
from connectors.payments import PaymentsConnector
from connectors.payments_mock import MockPaymentsProvider
from storage.experimental_durable_execution import (
    ExecutionStatus as X,
    ExecutionTask,
    ExecutionWorker,
    InMemoryTaskQueue,
    relay_once,
)
from storage.experimental_recovery_worker import (
    Reconciler,
    ReconcileStatus as R,
    account_operations,
)
from storage import durable_lifecycle as L
from tests.integration.lifecycle_fixture import DisposableLifecycleDatabase
from tests.integration.profiles_fixture import PAYMENTS_EVENTUAL_FAST
from tests.integration.timing import margin_for, wait_until_past


LEASE = 1
EXPIRE = LEASE + 0.3


class SimulatedCrash(BaseException):
    """Process death: nothing in the worker may catch it."""


def at(point, action):
    def hook(where, connection):
        if where == point:
            action(connection)
    return hook


def crash(_connection):
    raise SimulatedCrash()


def lose_ack(_connection):
    raise ConnectionError("synthetic lost acknowledgement")


def drop(connection):
    connection.close()


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class CrashRecoveryTests(unittest.TestCase):

    def setUp(self):
        self.db = DisposableLifecycleDatabase()
        self.addCleanup(self.db.cleanup)
        self.db.start()

        self.schema = self.db.schema
        self.ledger = EffectsLedger()
        self.provider = MockPaymentsProvider(ledger=self.ledger)
        self.contract_version = "v1"
        self.admitted = []
        self.addCleanup(self.assert_invariants)

    # -- helpers -------------------------------------------------------

    def use_provider(self, **options):
        # The registered contract must describe the provider's lookup.
        self.provider = MockPaymentsProvider(ledger=self.ledger, **options)
        if options.get("lookup_semantics") is LookupSemantics.EVENTUAL:
            self.contract_version = PAYMENTS_EVENTUAL_FAST.version

    def admit(self):
        operation_id = self.db.admit(
            request_key=f"refund-{len(self.admitted) + 1}",
            contract_version=self.contract_version,
        )
        self.admitted.append(operation_id)
        return operation_id

    def key(self, operation_id):
        return self.db.provider_key(operation_id)

    def worker(self, hook=None, lease=LEASE):
        return ExecutionWorker(
            connect=lambda: self.db.connect(self.db.worker_a),
            schema=self.schema,
            connectors=[PaymentsConnector(self.provider)],
            lease_seconds=lease, fault_hook=hook,
        )

    def reconciler(self, **options):
        return Reconciler(
            connect=lambda: self.db.connect(self.db.recoverer),
            schema=self.schema,
            connectors=[PaymentsConnector(self.provider)], **options,
        )

    def execute(self, operation_id, hook=None, lease=LEASE):
        return self.worker(hook, lease).execute(
            ExecutionTask(operation_id, uuid4(), uuid4())
        )

    def state(self, operation_id):
        row = self.db.admin.execute(sql.SQL(
            "SELECT state, seq FROM {} WHERE operation_id = %s"
        ).format(self.db.table("safeact_operation_current_state")),
            (operation_id,),
        ).fetchone()
        return S(row[0]), row[1]

    def calls(self, operation_id):
        key = self.key(operation_id)
        return sum(1 for k, _ in self.ledger.snapshot()[0] if k == key)

    def effects(self, operation_id):
        return len(self.ledger.effects_for(self.key(operation_id)))

    def accounting(self):
        connection = self.db.connect(self.db.recoverer)
        return account_operations(connection, self.schema)

    def assert_invariants(self):
        # Runs after every test (cleanups run LIFO, before teardown
        # of the fixture registered earlier).
        accounting = self.accounting()
        self.assertEqual(accounting.total, len(self.admitted))

        for operation_id in self.admitted:
            self.assertLessEqual(self.effects(operation_id), 1)

            seqs = [seq for (seq,) in self.db.admin.execute(sql.SQL(
                "SELECT seq FROM {} WHERE operation_id = %s ORDER BY seq"
            ).format(self.db.table("safeact_operation_transitions")),
                (operation_id,),
            ).fetchall()]
            self.assertEqual(seqs, list(range(1, len(seqs) + 1)))

    # -- worker crashes -------------------------------------------------

    def test_crash_after_claim_never_dispatches_and_exits_after_lease(self):
        operation_id = self.admit()

        with self.assertRaises(SimulatedCrash):
            self.execute(operation_id, at("after_claim_commit", crash))

        self.assertEqual(self.state(operation_id), (S.CLAIMED, 1))

        # Not republished, not reclaimable while the claim is held.
        queue = InMemoryTaskQueue()
        relay = self.db.connect(self.db.worker_b)
        self.assertEqual(relay_once(relay, self.schema, queue), ())
        self.assertEqual(
            self.execute(operation_id).status, X.NOT_CLAIMABLE
        )

        # After the claim lease, the abandoned claim leaves CLAIMED
        # (0006). No attempt was made, so it becomes retry-eligible.
        time.sleep(EXPIRE)
        (moved,) = self.reconciler().reconcile_once()
        self.assertEqual(moved.state, S.RETRY_ELIGIBLE)

        self.assertEqual(self.calls(operation_id), 0)
        self.assertEqual(self.accounting().retry_eligible, 1)

    def test_crash_after_fence_with_strong_lookup_is_proven_failed(self):
        operation_id = self.admit()

        with self.assertRaises(SimulatedCrash):
            self.execute(operation_id, at("after_fence_commit", crash))
        self.assertEqual(self.state(operation_id), (S.DISPATCHING, 2))

        # Lease still active: investigation must wait.
        (waiting,) = self.reconciler().reconcile_once()
        self.assertEqual(waiting.status, R.WAITING)

        time.sleep(EXPIRE)
        (result,) = self.reconciler().reconcile_once()

        self.assertEqual(result.decision, D.CONFIRM_NOT_APPLIED)
        self.assertEqual(self.state(operation_id), (S.FAILED_CONFIRMED, 5))
        self.assertEqual(self.calls(operation_id), 0)

    def test_crash_after_fence_with_eventual_lookup_never_fails(self):
        # NOT_FOUND is not proof: verify later, then escalate.
        self.use_provider(lookup_semantics=LookupSemantics.EVENTUAL)
        operation_id = self.admit()

        with self.assertRaises(SimulatedCrash):
            self.execute(operation_id, at("after_fence_commit", crash))
        time.sleep(EXPIRE)

        (later,) = self.reconciler().reconcile_once()
        self.assertEqual(later.decision, D.VERIFY_LATER)
        self.assertEqual(self.state(operation_id), (S.UNKNOWN, 5))

        connection = self.db.connect(self.db.recoverer)
        deadline = L.investigation_status(
            connection, self.schema, operation_id
        ).investigation_deadline
        connection.commit()
        wait_until_past(
            connection, deadline,
            margin_for(PAYMENTS_EVENTUAL_FAST.investigation_window),
        )
        (escalated,) = self.reconciler().reconcile_once()
        self.assertEqual(escalated.decision, D.ESCALATE_UNRESOLVED)
        self.assertEqual(
            self.state(operation_id), (S.ESCALATED_UNRESOLVED, 7)
        )
        self.assertEqual(self.calls(operation_id), 0)

        accounting = self.accounting()
        self.assertEqual(accounting.unresolved, 1)
        self.assertEqual(accounting.completed, 0)

    def test_unavailable_lookup_is_inconclusive_not_failure(self):
        operation_id = self.admit()

        with self.assertRaises(SimulatedCrash):
            self.execute(operation_id, at("after_fence_commit", crash))
        time.sleep(EXPIRE)

        self.provider.lookup_unavailable = True
        (result,) = self.reconciler().reconcile_once()

        self.assertEqual(result.decision, D.VERIFY_LATER)
        self.assertEqual(self.state(operation_id)[0], S.UNKNOWN)

    def test_crash_after_effect_before_outcome_recovers_succeeded(self):
        operation_id = self.admit()

        with self.assertRaises(SimulatedCrash):
            self.execute(operation_id, at("after_provider_call", crash))
        self.assertEqual(self.state(operation_id), (S.DISPATCHING, 2))
        self.assertEqual(self.effects(operation_id), 1)

        time.sleep(EXPIRE)
        (result,) = self.reconciler().reconcile_once()

        self.assertEqual(result.decision, D.CONFIRM_APPLIED)
        self.assertEqual(self.state(operation_id), (S.SUCCEEDED, 5))
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (1, 1)
        )

    # -- commit uncertainty ---------------------------------------------

    def test_lost_claim_ack_stops_without_dispatch(self):
        operation_id = self.admit()

        result = self.execute(
            operation_id, at("after_claim_commit", lose_ack)
        )

        self.assertEqual((result.status, result.stage), (X.UNCERTAIN, "claim"))
        self.assertEqual(self.state(operation_id), (S.CLAIMED, 1))
        self.assertEqual(self.calls(operation_id), 0)

    def test_lost_outcome_ack_is_observed_not_rewritten(self):
        operation_id = self.admit()

        result = self.execute(
            operation_id, at("after_outcome_commit", lose_ack)
        )

        self.assertEqual(
            (result.status, result.stage), (X.SUCCEEDED, "observed")
        )
        self.assertEqual(self.state(operation_id), (S.SUCCEEDED, 3))
        self.assertEqual(self.db.count(
            "safeact_attempt_outcomes", operation_id=operation_id
        ), 1)
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (1, 1)
        )

    def test_connection_drop_mid_outcome_commit_repeats_write_once(self):
        operation_id = self.admit()

        result = self.execute(
            operation_id, at("before_outcome_commit", drop)
        )

        self.assertEqual(
            (result.status, result.stage), (X.SUCCEEDED, "outcome_retry")
        )
        self.assertEqual(self.state(operation_id), (S.SUCCEEDED, 3))
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (1, 1)
        )

    def test_dropped_outcome_with_failed_observation_waits_for_recovery(self):
        operation_id = self.admit()
        connects = []

        def connect():
            connects.append(1)
            if len(connects) > 1:
                raise ConnectionError("database unreachable")
            return self.db.connect(self.db.worker_a)

        worker = ExecutionWorker(
            connect=connect, schema=self.schema,
            connectors=[PaymentsConnector(self.provider)],
            lease_seconds=LEASE,
            fault_hook=at("before_outcome_commit", drop),
        )
        result = worker.execute(ExecutionTask(operation_id, uuid4(), uuid4()))

        self.assertEqual((result.status, result.stage), (X.UNCERTAIN, "outcome"))
        self.assertTrue(result.provider_called)
        self.assertEqual(self.state(operation_id), (S.DISPATCHING, 2))

        time.sleep(EXPIRE)
        (recovered,) = self.reconciler().reconcile_once()

        self.assertEqual(recovered.decision, D.CONFIRM_APPLIED)
        self.assertEqual(self.state(operation_id)[0], S.SUCCEEDED)
        self.assertEqual(self.calls(operation_id), 1)

    def test_reconciler_lost_ack_resumes_on_next_pass(self):
        operation_id = self.admit()

        with self.assertRaises(SimulatedCrash):
            self.execute(operation_id, at("after_provider_call", crash))
        time.sleep(EXPIRE)

        (uncertain,) = self.reconciler(
            fault_hook=at("after_to_verifying_commit", lose_ack)
        ).reconcile_once()
        self.assertEqual(uncertain.status, R.UNCERTAIN)
        self.assertEqual(self.state(operation_id), (S.VERIFYING, 4))

        (resolved,) = self.reconciler().reconcile_once()
        self.assertEqual(resolved.decision, D.CONFIRM_APPLIED)
        self.assertEqual(self.state(operation_id), (S.SUCCEEDED, 5))

    # -- stale worker ------------------------------------------------------

    def test_stale_worker_reply_is_evidence_not_overwrite(self):
        self.use_provider(lookup_semantics=LookupSemantics.EVENTUAL)
        self.provider.hide_new_effects = True
        operation_id = self.admit()
        decisions = []

        def lease_expires_mid_call(_connection):
            time.sleep(EXPIRE)
            decisions.extend(self.reconciler().reconcile_once())

        result = self.execute(
            operation_id, at("after_provider_call", lease_expires_mid_call)
        )

        # Recovery decided first (lookup lagging: verify later).
        self.assertEqual([d.decision for d in decisions], [D.VERIFY_LATER])
        self.assertEqual(result.status, X.LATE_EVIDENCE)
        self.assertEqual(self.state(operation_id), (S.UNKNOWN, 5))

        late = self.db.admin.execute(sql.SQL(
            "SELECT outcome, evidence_kind, details->>'late' FROM {} "
            "WHERE operation_id = %s AND details ? 'late'"
        ).format(self.db.table("safeact_attempt_outcomes")),
            (operation_id,),
        ).fetchall()
        self.assertEqual(late, [("confirmed_applied", "applied", "true")])

        # The next pass reconciles the recorded receipt.
        (resolved,) = self.reconciler().reconcile_once()
        self.assertEqual(resolved.decision, D.CONFIRM_APPLIED)
        self.assertEqual(self.state(operation_id), (S.SUCCEEDED, 7))
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (1, 1)
        )

    # -- RETRY_ELIGIBLE is reclaimed once, after proof (0007) ---------------

    def test_retry_after_proof_is_relayed_and_dispatched_once(self):
        operation_id = self.admit()

        with self.assertRaises(SimulatedCrash):
            self.execute(operation_id, at("after_fence_commit", crash))
        time.sleep(EXPIRE)

        (result,) = self.reconciler(retry_authorized=True).reconcile_once()
        self.assertEqual(result.decision, D.RETRY_AFTER_PROOF)
        self.assertEqual(self.state(operation_id), (S.RETRY_ELIGIBLE, 5))
        self.assertEqual(self.accounting().retry_eligible, 1)

        queue = InMemoryTaskQueue()
        relay = self.db.connect(self.db.worker_b)
        (task,) = relay_once(relay, self.schema, queue)
        self.assertEqual(task.operation_id, operation_id)

        # Reclaim, a second attempt under the same key, success.
        self.assertEqual(self.execute(operation_id).status, X.SUCCEEDED)
        self.assertEqual(self.state(operation_id), (S.SUCCEEDED, 8))
        # A duplicate delivery of the retry task changes nothing.
        self.assertEqual(
            self.execute(operation_id).status, X.NOT_CLAIMABLE
        )
        self.assertEqual(self.db.count(
            "safeact_attempts", operation_id=operation_id
        ), 2)
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (1, 1)
        )

    # -- mixed run accounting ----------------------------------------------

    def test_every_admitted_operation_is_accounted_for(self):
        ids = [self.admit() for _ in range(6)]

        self.execute(ids[0])                                   # SUCCEEDED
        self.provider.reject_next = "rej_1"
        self.execute(ids[1])                                   # FAILED
        self.provider.fail_after_effect = True
        self.execute(ids[2])                                   # UNKNOWN
        with self.assertRaises(SimulatedCrash):
            self.execute(ids[3], at("after_claim_commit", crash))   # CLAIMED, then exits
        with self.assertRaises(SimulatedCrash):
            self.execute(ids[4], at("after_provider_call", crash))  # DISPATCHING
        # ids[5] stays RECORDED.

        time.sleep(EXPIRE)
        self.reconciler().reconcile_once()

        accounting = self.accounting()
        self.assertEqual(accounting.total, 6)
        self.assertEqual(dict(accounting.by_state), {
            S.SUCCEEDED: 3,          # ids 0, 2 (lookup), 4 (lookup)
            S.FAILED_CONFIRMED: 1,
            S.RETRY_ELIGIBLE: 1,     # id 3: abandoned claim (0006)
            S.RECORDED: 1,
        })
        self.assertEqual(accounting.completed, 4)
        self.assertEqual(accounting.retry_eligible, 1)
        self.assertEqual(accounting.in_flight, 2)  # RECORDED, RETRY_ELIGIBLE
        self.assertEqual(accounting.unresolved, 0)


if __name__ == "__main__":
    unittest.main()
