"""Mock refund vertical slice: admission -> relay -> claim -> fence ->
fake payments provider -> outcome persistence (acceptance check A1).

Disposable schema and the in-memory payments mock only. Provider
effects are counted by the mock itself, never from worker reports.
"""

import json
import os
import unittest

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg
from psycopg import sql

from core.lifecycle import LifecycleState as S
from connectors.ledger import EffectsLedger
from connectors.payments import PaymentsConnector
from connectors.payments_mock import MockPaymentsProvider
from storage import durable_lifecycle as L
from storage.experimental_durable_execution import (
    ExecutionStatus as X,
    ExecutionTask,
    InMemoryTaskQueue,
    ExecutionWorker,
    relay_once,
)
from storage.submission import record_intent
from tests.integration.lifecycle_fixture import (
    DisposableLifecycleDatabase,
    commit,
)


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class DurableExecutionHappyPathTests(unittest.TestCase):

    def setUp(self):
        self.db = DisposableLifecycleDatabase()
        self.addCleanup(self.db.cleanup)
        self.db.start()

        self.schema = self.db.schema
        self.ledger = EffectsLedger()
        self.provider = MockPaymentsProvider(ledger=self.ledger)
        self.queue = InMemoryTaskQueue()
        self.relay_connection = self.db.connect(self.db.worker_b)
        self._keys = 0

    # -- helpers -------------------------------------------------------

    def admit(self, **changes):
        self._keys += 1
        changes.setdefault("request_key", f"refund-{self._keys}")
        return self.db.admit(**changes)

    def worker(self, role=None, **options):
        role = role or self.db.worker_a
        return ExecutionWorker(
            connect=lambda: self.db.connect(role),
            schema=self.schema,
            connectors=[PaymentsConnector(self.provider)],
            **options,
        )

    def relay(self):
        return relay_once(self.relay_connection, self.schema, self.queue)

    def state(self, operation_id):
        row = self.db.admin.execute(sql.SQL(
            "SELECT state, seq FROM {} WHERE operation_id = %s"
        ).format(self.db.table("safeact_operation_current_state")),
            (operation_id,),
        ).fetchone()
        return S(row[0]), row[1]

    def effects(self):
        return len(self.ledger.snapshot()[1])

    def calls(self):
        return len(self.ledger.snapshot()[0])

    def audit_events(self, operation_id):
        return [event for (event,) in self.db.admin.execute(sql.SQL(
            "SELECT event_type FROM {} WHERE operation_id = %s "
            "ORDER BY audit_id"
        ).format(self.db.table("safeact_audit_events")),
            (operation_id,),
        ).fetchall()]

    def evidence(self, operation_id):
        return self.db.admin.execute(sql.SQL(
            "SELECT outcome, evidence_kind, evidence_reference FROM {} "
            "WHERE operation_id = %s ORDER BY created_at"
        ).format(self.db.table("safeact_attempt_outcomes")),
            (operation_id,),
        ).fetchall()

    # -- happy path ------------------------------------------------------

    def test_mock_refund_happy_path(self):
        operation_id = self.admit()

        tasks = self.relay()
        self.assertEqual([t.operation_id for t in tasks], [operation_id])

        results = self.worker().run_until_idle(self.queue)

        self.assertEqual(
            [(r.status, r.provider_called) for r in results],
            [(X.SUCCEEDED, True)],
        )
        self.assertEqual(self.state(operation_id), (S.SUCCEEDED, 3))
        self.assertEqual((self.calls(), self.effects()), (1, 1))

        (key, params), = self.ledger.snapshot()[0]
        request = self.db.request(request_key="refund-1")
        self.assertEqual(key, request.provider_idempotency_key)
        self.assertEqual(params, request.payload)

        refund_id = self.ledger.effects_for(key)[0]
        self.assertEqual(self.evidence(operation_id), [
            ("confirmed_applied", "applied", refund_id),
        ])
        self.assertEqual(self.audit_events(operation_id), [
            "INTENT_RECORDED",
            "LIFECYCLE_CLAIMED",
            "DISPATCH_RISK_COMMITTED",
            "ATTEMPT_EVIDENCE_RECORDED",
            "LIFECYCLE_DISPATCH_OUTCOME",
        ])
        self.assertEqual(self.db.count(
            "safeact_outbox_deliveries", operation_id=operation_id
        ), 1)

        # Nothing left to relay once claimed.
        self.assertEqual(self.relay(), ())

    # -- A1: no dispatch before durable intent ---------------------------

    def test_provider_sees_committed_intent_and_fence_on_every_call(self):
        observations = []
        settings = dict(self.db.settings)

        def spy(key, params):
            # An independent session sees only committed data.
            with psycopg.connect(**settings, autocommit=True) as probe:
                row = probe.execute(sql.SQL("""
                    SELECT o.operation_id, o.payload_canonical,
                           s.state, s.attempt_id,
                           (SELECT count(*) FROM {audit} a
                            WHERE a.operation_id = o.operation_id
                              AND a.event_type = 'INTENT_RECORDED'),
                           (SELECT count(*) FROM {outbox} e
                            WHERE e.operation_id = o.operation_id)
                    FROM {ops} o
                    JOIN {current} s USING (operation_id)
                    WHERE o.provider_idempotency_key = %s
                """).format(
                    ops=self.db.table("safeact_operations"),
                    current=self.db.table(
                        "safeact_operation_current_state"
                    ),
                    audit=self.db.table("safeact_audit_events"),
                    outbox=self.db.table("safeact_outbox_events"),
                ), (key,)).fetchone()
            observations.append((row, params))

        self.provider.before_call = spy

        operation_ids = [self.admit() for _ in range(3)]
        self.relay()
        results = self.worker().run_until_idle(self.queue)

        self.assertEqual(
            [r.status for r in results], [X.SUCCEEDED] * 3
        )
        self.assertEqual(len(observations), 3)

        for row, params in observations:
            with self.subTest(operation=row and row[0]):
                self.assertIsNotNone(row)
                operation_id, payload, state, attempt, audit, outbox = row
                self.assertIn(operation_id, operation_ids)
                self.assertEqual(json.loads(payload), params)
                self.assertEqual(state, "DISPATCHING")
                self.assertIsNotNone(attempt)
                self.assertEqual((audit, outbox), (1, 1))

    def test_unadmitted_operation_never_reaches_provider(self):
        task = ExecutionTask(uuid4(), uuid4(), uuid4())

        result = self.worker().execute(task)

        self.assertEqual(result.status, X.NOT_CLAIMABLE)
        self.assertFalse(result.provider_called)
        self.assertEqual(self.calls(), 0)

    def test_uncommitted_or_rolled_back_intent_never_dispatches(self):
        with self.db.engine.connect() as admission:
            transaction = admission.begin()
            receipt = record_intent(
                admission, self.db.request(request_key="in-flight")
            )

            # Visible to nobody else yet.
            self.assertEqual(self.relay(), ())
            result = self.worker().execute(ExecutionTask(
                receipt.operation_id, uuid4(), uuid4()
            ))
            self.assertEqual(result.status, X.NOT_CLAIMABLE)

            transaction.rollback()

        self.assertEqual(self.relay(), ())
        result = self.worker().execute(ExecutionTask(
            receipt.operation_id, uuid4(), uuid4()
        ))
        self.assertEqual(result.status, X.NOT_CLAIMABLE)
        self.assertEqual(self.calls(), 0)
        self.assertEqual(self.db.count("safeact_operations"), 0)

    def test_fence_commit_failure_never_calls_provider(self):
        cases = {
            # Raised before COMMIT: nothing committed.
            "lost_before": ("before_fence_commit", ConnectionError(), S.CLAIMED),
            # Real connection drop before COMMIT.
            "dropped": ("before_fence_commit", None, S.CLAIMED),
            # COMMIT happened, acknowledgement lost.
            "lost_ack": ("after_fence_commit", ConnectionError(), S.DISPATCHING),
        }

        for name, (point, error, expected) in cases.items():
            with self.subTest(case=name):
                operation_id = self.admit()

                def hook(at, connection, point=point, error=error):
                    if at != point:
                        return
                    if error is None:
                        connection.close()
                    else:
                        raise error

                result = self.worker(fault_hook=hook).execute(
                    ExecutionTask(operation_id, uuid4(), uuid4())
                )

                self.assertEqual(result.status, X.UNCERTAIN)
                self.assertEqual(result.stage, "fence")
                self.assertFalse(result.provider_called)
                self.assertEqual(self.state(operation_id)[0], expected)

        self.assertEqual(self.calls(), 0)

    # -- duplicate delivery and contention -------------------------------

    def test_double_publish_produces_one_claim_and_one_effect(self):
        operation_id = self.admit()

        self.relay()
        self.relay()
        self.assertEqual(len(self.queue), 2)

        results = self.worker().run_until_idle(self.queue)

        self.assertEqual(
            sorted(r.status.value for r in results),
            sorted([X.SUCCEEDED.value, X.NOT_CLAIMABLE.value]),
        )
        self.assertEqual((self.calls(), self.effects()), (1, 1))
        self.assertEqual(self.db.count(
            "safeact_outbox_deliveries", operation_id=operation_id
        ), 2)
        self.assertEqual(self.db.count(
            "safeact_operation_transitions", operation_id=operation_id
        ), 3)

    def test_concurrent_workers_on_one_task_have_one_claimant(self):
        operation_id = self.admit()
        task = ExecutionTask(operation_id, uuid4(), uuid4())
        workers = [
            self.worker(self.db.worker_a if i % 2 else self.db.worker_b)
            for i in range(6)
        ]
        barrier = Barrier(len(workers))

        def run(worker):
            barrier.wait(timeout=10)
            return worker.execute(task)

        with ThreadPoolExecutor(max_workers=len(workers)) as pool:
            results = list(pool.map(run, workers))

        statuses = [r.status for r in results]
        self.assertEqual(statuses.count(X.SUCCEEDED), 1)
        self.assertEqual(statuses.count(X.NOT_CLAIMABLE), 5)
        self.assertEqual((self.calls(), self.effects()), (1, 1))

    # -- provider outcomes -------------------------------------------------

    def run_one(self, **changes):
        operation_id = self.admit(**changes)
        result = self.worker().execute(
            ExecutionTask(operation_id, uuid4(), uuid4())
        )
        return operation_id, result

    def test_definitive_rejection_is_failed_with_proof(self):
        self.provider.reject_next = "rej_charge_already_refunded"

        operation_id, result = self.run_one()

        self.assertEqual(result.status, X.FAILED_CONFIRMED)
        self.assertEqual(
            self.state(operation_id), (S.FAILED_CONFIRMED, 3)
        )
        self.assertEqual(self.evidence(operation_id), [(
            "proven_not_applied", "proven_not_applied",
            "rej_charge_already_refunded",
        )])
        self.assertEqual(self.effects(), 0)

    def test_no_response_is_unknown_whether_or_not_applied(self):
        # The worker cannot tell these apart, and must not try.
        for switch, effects in (
            ("fail_before_effect", 0),
            ("fail_after_effect", 1),
        ):
            with self.subTest(switch=switch):
                before = self.effects()
                setattr(self.provider, switch, True)

                operation_id, result = self.run_one()

                self.assertEqual(result.status, X.UNKNOWN)
                self.assertEqual(self.state(operation_id), (S.UNKNOWN, 3))
                self.assertEqual(self.evidence(operation_id), [
                    ("may_have_dispatched", None, None),
                ])
                self.assertEqual(self.effects() - before, effects)

    def test_invalid_durable_intent_is_never_dispatched(self):
        for changes in (
            {"connector_id": "mock-crm"},
            {"action_type": "create_case"},
            # A payments intent without a key can no longer be admitted:
            # keys are derived server-side (0008, test_provider_keys).
        ):
            with self.subTest(changes=changes):
                operation_id, result = self.run_one(**changes)

                self.assertEqual(result.status, X.INVALID_INTENT)
                self.assertFalse(result.provider_called)
                # Visible, claimed, never dispatched.
                self.assertEqual(self.state(operation_id), (S.CLAIMED, 1))
                self.assertEqual(self.db.count(
                    "safeact_attempts", operation_id=operation_id
                ), 0)

        self.assertEqual(self.calls(), 0)

    def test_claim_returns_durable_intent_not_task_data(self):
        operation_id = self.admit(request_key="intent-check")
        worker = self.db.connect(self.db.worker_a)

        held = commit(worker, lambda: L.claim(
            worker, self.schema, operation_id, lease_seconds=60,
        ))

        request = self.db.request(request_key="intent-check")
        self.assertEqual(held.intent.connector_id, request.connector_id)
        self.assertEqual(
            json.loads(held.intent.payload_canonical), request.payload
        )
        self.assertEqual(
            held.intent.provider_idempotency_key,
            request.provider_idempotency_key,
        )


if __name__ == "__main__":
    unittest.main()
