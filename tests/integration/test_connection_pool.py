"""Actual native pooled sessions, reset, fork and execution failure behavior."""
import os
import select
import signal
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
from uuid import uuid4

import psycopg

from connectors.email import EmailConnector
from connectors.email_mock import MockEmailProvider
from connectors.ledger import EffectsLedger
from connectors.payments import PaymentsConnector
from connectors.payments_mock import MockPaymentsProvider
from core.lifecycle import LifecycleState as S
from core.recovery import DecisionKind as D
from storage import durable_lifecycle as lifecycle
from storage.connection_pool import ConnectionPool
from storage.experimental_durable_execution import ExecutionStatus as X, ExecutionTask, ExecutionWorker
from storage.experimental_recovery_worker import Reconciler
from storage.experimental_workflow import WorkflowAdvancer
from tests.integration.lifecycle_fixture import DisposableLifecycleDatabase

ENABLED = all(os.environ.get(key)=="1" for key in
              ("SAFEACT_RUN_DB_INTEGRATION", "SAFEACT_RUN_DISPOSABLE_DB_TESTS"))


@unittest.skipUnless(ENABLED, "Disposable PostgreSQL pool checks require explicit opt-in")
class ConnectionPoolTests(unittest.TestCase):
    def setUp(self):
        self.db = DisposableLifecycleDatabase()
        self.addCleanup(self.db.cleanup)
        self.db.start()
        self.pool = self.make_pool(self.db.worker_a)
        self.ledger = EffectsLedger()
        self.payment = MockPaymentsProvider(ledger=self.ledger)
        self.email = MockEmailProvider(ledger=self.ledger)
        self.connectors = [PaymentsConnector(self.payment), EmailConnector(self.email)]

    def make_pool(self, role, **kwargs):
        settings = dict(self.db.settings, user=role, password=self.db.passwords[role])
        pool = ConnectionPool(settings, size=kwargs.pop("size", 1), **kwargs)
        self.addCleanup(pool.close)
        return pool

    def execute(self, operation, hook=None, pool=None):
        return ExecutionWorker(connect=lambda: self.db.connect(self.db.worker_a),
            connection_scope=(pool or self.pool).connection, schema=self.db.schema,
            connectors=self.connectors, fault_hook=hook).execute(
                ExecutionTask(operation, uuid4(), uuid4()))

    def state(self, operation):
        return lifecycle.current_state(self.db.admin, self.db.schema, operation).state

    def counts(self, operation, email=False):
        key = str(operation) if email else self.db.provider_key(operation)
        return len(self.ledger.calls_for(key)), len(self.ledger.effects_for(key))

    def test_reuses_native_connections_and_keeps_automatic_preparation(self):
        pids = set()
        for _ in range(8):
            with self.pool.connection() as connection:
                self.assertIs(type(connection), psycopg.Connection)
                self.assertEqual(connection.info.user, self.db.worker_a)
                pids.add(connection.info.backend_pid)
                self.assertEqual(connection.execute("SELECT %s::integer", (7,)).fetchone()[0], 7)
                connection.commit()
        self.assertEqual(len(pids), 1)
        with self.pool.connection() as connection:
            self.assertGreater(connection.execute("SELECT count(*) FROM pg_prepared_statements").fetchone()[0], 0)

    def test_unfinished_transaction_and_session_state_do_not_leak(self):
        with self.pool.connection() as connection:
            connection.execute("CREATE TEMP TABLE pool_private_probe(value integer)")
            connection.execute("SET application_name = 'pool-private-probe'")
            connection.execute("SELECT pg_advisory_lock(86547321)")
            connection.commit()
            xid = connection.execute("SELECT pg_current_xact_id()::text").fetchone()[0]
            connection.execute("SET LOCAL statement_timeout = '900ms'")
        self.assertEqual(self.db.admin.execute("SELECT pg_xact_status(%s::xid8)", (xid,)).fetchone()[0], "aborted")
        with self.pool.connection() as connection:
            self.assertIs(connection.info.transaction_status, psycopg.pq.TransactionStatus.IDLE)
            self.assertNotEqual(connection.execute("SHOW application_name").fetchone()[0], "pool-private-probe")
            self.assertIsNone(connection.execute("SELECT to_regclass('pg_temp.pool_private_probe')").fetchone()[0])
            self.assertFalse(connection.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory')").fetchone()[0])
            self.assertEqual(connection.execute("SELECT session_user").fetchone()[0], self.db.worker_a)

    def test_two_roles_never_share_a_session(self):
        other = self.make_pool(self.db.worker_b)
        with self.pool.connection() as left, other.connection() as right:
            self.assertNotEqual(left.info.backend_pid, right.info.backend_pid)
            self.assertEqual(left.execute("SELECT session_user").fetchone()[0], self.db.worker_a)
            self.assertEqual(right.execute("SELECT session_user").fetchone()[0], self.db.worker_b)

    def test_bound_and_wait_timeout_are_enforced(self):
        pool = self.make_pool(self.db.worker_b, timeout=.05)
        with pool.connection():
            with ThreadPoolExecutor(max_workers=1) as executor:
                def blocked():
                    with pool.connection():
                        raise AssertionError("Pool exceeded its bound")
                with self.assertRaisesRegex(psycopg.OperationalError, "Database pool is busy"):
                    executor.submit(blocked).result(timeout=2)

    def test_closed_connection_is_discarded(self):
        with self.pool.connection() as connection:
            old = connection.info.backend_pid
            connection.close()
        with self.pool.connection() as connection:
            self.assertNotEqual(connection.info.backend_pid, old)

    def test_shutdown_during_reset_does_not_return_a_live_session(self):
        entered, release = threading.Event(), threading.Event()
        original_reset = self.pool._reset
        observed = {}
        def reset(connection):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Shutdown test synchronization timed out")
            original_reset(connection)
        def borrow():
            with self.pool.connection() as connection:
                observed['connection'] = connection
                observed['pid'] = connection.info.backend_pid
        with mock.patch.object(self.pool, "_reset", side_effect=reset), ThreadPoolExecutor(max_workers=1) as executor:
            task = executor.submit(borrow)
            try:
                self.assertTrue(entered.wait(5))
                self.pool.close()
            finally:
                release.set()
            task.result(timeout=5)
        self.assertTrue(observed['connection'].closed)
        deadline = time.monotonic()+2
        while self.db.admin.execute("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=%s)", (observed['pid'],)).fetchone()[0]:
            self.assertLess(time.monotonic(), deadline, "Closed pool retained a database session")
            time.sleep(.01)

    def test_server_terminated_idle_connection_is_replaced_before_use(self):
        with self.pool.connection() as connection:
            old = connection.info.backend_pid
        self.db.admin.execute("SELECT pg_terminate_backend(%s)", (old,))
        with self.pool.connection() as connection:
            self.assertNotEqual(connection.info.backend_pid, old)
            self.assertEqual(connection.execute("SELECT session_user").fetchone()[0], self.db.worker_a)

    def test_reset_failure_discards_connection_without_masking_body_error(self):
        with mock.patch.object(self.pool, "_reset", side_effect=psycopg.OperationalError("synthetic reset failure")):
            with self.assertLogs("safeact.database", level="WARNING"):
                with self.assertRaisesRegex(ValueError, "body failure"):
                    with self.pool.connection() as connection:
                        old = connection.info.backend_pid
                        raise ValueError("body failure")
        with self.pool.connection() as connection:
            self.assertNotEqual(connection.info.backend_pid, old)

    def test_fork_cannot_use_or_close_parent_pool(self):
        with self.pool.connection() as connection:
            old = connection.info.backend_pid
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            try:
                self.pool.close()
                with self.pool.connection():
                    os.write(write_fd, b"wrong")
            except RuntimeError:
                os.write(write_fd, b"refused")
            finally:
                os._exit(0)
        os.close(write_fd)
        try:
            ready, _, _ = select.select([read_fd], [], [], 5)
            if not ready:
                os.kill(child, signal.SIGKILL)
            self.assertTrue(ready, "Child pool refusal did not complete")
            self.assertEqual(os.read(read_fd, 32), b"refused")
        finally:
            os.close(read_fd)
            os.waitpid(child, 0)
        with self.pool.connection() as connection:
            self.assertEqual(connection.info.backend_pid, old)

    def test_worker_keeps_three_commits_and_duplicate_delivery_is_refused(self):
        operation = self.db.admit()
        stages = []
        self.assertIs(self.execute(operation, lambda point, _: stages.append(point)).status, X.SUCCEEDED)
        self.assertEqual([stage for stage in stages if stage.startswith("after_") and stage.endswith("_commit")],
                         ["after_claim_commit", "after_fence_commit", "after_outcome_commit"])
        self.assertIs(self.execute(operation).status, X.NOT_CLAIMABLE)
        self.assertEqual(self.counts(operation), (1, 1))

    def test_concurrent_duplicate_delivery_keeps_one_effect(self):
        pool = self.make_pool(self.db.worker_b, size=2)
        operation = self.db.admit()
        with ThreadPoolExecutor(max_workers=2) as executor:
            answers = list(executor.map(lambda _: self.execute(operation, pool=pool), range(2)))
        self.assertEqual({answer.status for answer in answers}, {X.SUCCEEDED, X.NOT_CLAIMABLE})
        self.assertEqual(self.counts(operation), (1, 1))

    def test_lost_claim_ack_never_sends(self):
        self.check_lost_ack("after_claim_commit", S.CLAIMED)

    def test_lost_fence_ack_never_sends(self):
        self.check_lost_ack("after_fence_commit", S.DISPATCHING)

    def check_lost_ack(self, point, expected):
        operation = self.db.admit()
        def hook(where, _connection):
            if where == point:
                raise ConnectionError("synthetic lost acknowledgement")
        self.assertIs(self.execute(operation, hook).status, X.UNCERTAIN)
        self.assertIs(self.state(operation), expected)
        self.assertEqual(self.counts(operation), (0, 0))

    def test_lost_outcome_ack_uses_fresh_observer_without_pool_deadlock(self):
        operation = self.db.admit()
        def hook(where, _connection):
            if where == "after_outcome_commit":
                raise ConnectionError("synthetic lost acknowledgement")
        answer = self.execute(operation, hook)
        self.assertEqual((answer.status, answer.stage), (X.SUCCEEDED, "observed"))
        self.assertEqual(self.counts(operation), (1, 1))
        self.assertEqual(self.db.count("safeact_attempt_outcomes", operation_id=operation), 1)

    def test_connection_drop_before_outcome_recovers_without_resend(self):
        operation = self.db.admit()
        def hook(where, connection):
            if where == "before_outcome_commit":
                connection.close()
        answer = self.execute(operation, hook)
        self.assertEqual((answer.status, answer.stage), (X.SUCCEEDED, "outcome_retry"))
        self.assertEqual(self.counts(operation), (1, 1))
        self.assertIs(self.execute(self.db.admit(request_key="after-drop")).status, X.SUCCEEDED)

    def test_ambiguous_email_escalates_without_resend_with_pooled_recovery(self):
        operation = self.db.admit(connector_id="mock-email", action_type="send_refund_confirmation",
            provider_idempotency_key=None, resource_id="pool-customer",
            payload=dict(to="customer@example.invalid", template="refund"))
        self.email.fail_after_effect = True
        self.assertIs(self.execute(operation).status, X.UNKNOWN)
        recovery_pool = self.make_pool(self.db.recoverer)
        reconciler = Reconciler(connect=lambda: self.db.connect(self.db.recoverer),
            connection_scope=recovery_pool.connection, schema=self.db.schema,
            connectors=self.connectors, retry_authorized=True)
        self.assertIs(reconciler.reconcile_once()[0].decision, D.ESCALATE_UNRESOLVED)
        self.assertEqual(reconciler.reconcile_once()[0].status.value, "skipped")
        self.assertIs(self.state(operation), S.ESCALATED_UNRESOLVED)
        self.assertEqual(self.counts(operation, email=True), (1, 1))

    def test_advancer_scan_returns_healthy_role_bound_lease(self):
        pool = self.make_pool(self.db.advancer)
        advancer = WorkflowAdvancer(connect=lambda: self.db.connect(self.db.advancer),
            connection_scope=pool.connection, schema=self.db.schema)
        self.assertEqual(advancer.advance_once(), [])
        self.assertEqual(advancer.advance_once(), [])
        with pool.connection() as connection:
            self.assertEqual(connection.execute("SELECT session_user").fetchone()[0], self.db.advancer)
