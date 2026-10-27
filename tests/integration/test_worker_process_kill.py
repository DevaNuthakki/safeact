"""Kill a real, separate Celery worker process (deferred from Milestone 2).

The worker is ``celery -A deploy.stack.worker worker --pool=prefork`` in
its own process group, with the relay, reconciler and HTTP mock
providers as separate processes too. A harness-only failpoint
(SAFEACT_STACK_FAILPOINT, disposable schemas only) parks the worker at a
chosen point; the test then sends SIGKILL, and starts a fresh worker
without the failpoint. Effects are counted by the mock providers'
independent ledger, through the harness-only admin port.

Cases:
1. killed after the claim committed, before the fence: the claim lease
   expires, CLAIMED -> RETRY_ELIGIBLE (no attempt was made), the work is
   claimed again and succeeds: one effect, one provider call;
2. killed after the provider applied the effect, before the outcome was
   written: the lease expires, the reconciler investigates (lookup)
   before any redispatch and confirms it: one effect, ONE provider call;
3. killed just before the outcome commit (the acknowledgement is lost
   with the process): recovered by operation identity, as in 2;
4. a prefork child killed while its parent lives: the pool replaces the
   child, Celery requeues the unacknowledged message
   (task_reject_on_worker_lost), the redelivered task finds the work
   already owned and does nothing, and the reconciler resolves it.

Every case leaves the projection equal to the log. Redis DB 10 only.
"""

import os
import signal
import tempfile
import time
import unittest
import uuid

from pathlib import Path

import httpx2
import psycopg
import redis
import sqlalchemy as sa

from psycopg import sql
from sqlalchemy.pool import NullPool

from deploy.demo.bootstrap import (
    Environment, credential_lines, rebuild, teardown_statements,
    write_credentials,
)
from deploy.demo.profiles import E2E_PROFILES
from deploy.stack.processes import ProcessStack
from storage.database import (
    local_admin_settings, local_database_url, local_redis_url,
)
from storage.state_projection import projection_drift
from storage.submission import IntentSubmission, submit_intent
from tests.integration.test_stack_processes import free_port


ENABLED = (
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1"
    and os.environ.get("SAFEACT_RUN_BROKER_TESTS") == "1"
)
REDIS_DB = 10


@unittest.skipUnless(ENABLED, "Worker kill tests are opt-in")
class WorkerProcessKillTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.redis = redis.Redis.from_url(local_redis_url(REDIS_DB))
        cls.redis.flushdb()
        cls.addClassCleanup(cls.redis.flushdb)

        cls.admin = psycopg.connect(**local_admin_settings(), autocommit=True)
        cls.env = Environment.disposable(uuid.uuid4().hex)
        cls.addClassCleanup(cls.teardown_env)
        role_passwords, _ = rebuild(cls.admin, cls.env, profiles=E2E_PROFILES)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        credentials = Path(cls.tmp.name) / "credentials"
        write_credentials(credentials, credential_lines(cls.env,
                                                        role_passwords))
        cls.engine = sa.create_engine(
            local_database_url(), poolclass=NullPool,
            connect_args={"options": f"-c search_path={cls.env.schema},public"},
        )
        cls.addClassCleanup(cls.engine.dispose)

        cls.stack = ProcessStack(
            credentials=credentials, redis_db=REDIS_DB,
            queue="safeact-kill-" + uuid.uuid4().hex[:8],
            provider_ports={k: free_port()
                            for k in ("payments", "crm", "email", "admin")},
            poll_seconds=0.2, concurrency=1, plain_providers=True,
            components=("providers", "relay", "reconciler"),
            log_dir=Path(cls.tmp.name) / "logs",
            extra_env={
                # Short lease and redelivery so a case takes seconds.
                "SAFEACT_STACK_LEASE_SECONDS": "3",
                "SAFEACT_STACK_VISIBILITY_TIMEOUT": "5",
            },
        )
        cls.addClassCleanup(cls.stack.stop)
        cls.stack.start()
        cls.ledger = httpx2.Client(base_url=cls.stack.admin_url, timeout=5)
        cls.addClassCleanup(cls.ledger.close)

    @classmethod
    def teardown_env(cls):
        drift = projection_drift(cls.admin, cls.env.schema)
        cls.admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE usename = ANY(%s)", (list(cls.env.roles),))
        with cls.admin.transaction():
            for statement in teardown_statements(cls.admin, cls.env):
                cls.admin.execute(statement)
        cls.admin.close()
        if drift:
            raise AssertionError(f"Current-state projection drift: {drift[:3]}")

    # -- helpers -------------------------------------------------------

    def worker(self, failpoint=None):
        """Start a fresh worker process, optionally armed with a failpoint."""
        self.stack.env.pop("SAFEACT_STACK_FAILPOINT", None)
        self.marker = Path(self.tmp.name) / f"marker-{uuid.uuid4().hex}"
        if failpoint:
            self.stack.env["SAFEACT_STACK_FAILPOINT"] = failpoint
            self.stack.env["SAFEACT_STACK_FAILPOINT_MARKER"] = str(self.marker)
        return self.stack.spawn("worker")

    def stop_worker(self):
        process = self.stack.processes.get("worker")
        if process is not None and process.poll() is None:
            self.stack.kill("worker")

    def admit(self):
        receipt = submit_intent(self.engine, IntentSubmission(
            tenant_id="kill", actor_id="kill-test",
            request_key="kill-" + uuid.uuid4().hex,
            action_type="refund_payment", resource_id="ch_kill",
            provider_account_id="mock-payments-account",
            connector_id="mock-payments", contract_version="v1",
            payload={"charge_id": "ch_kill", "amount_minor": 1},
        ))
        return receipt.operation_id

    def state(self, operation_id):
        return self.admin.execute(sql.SQL(
            "SELECT state FROM {}.safeact_state_projection "
            "WHERE operation_id = %s"
        ).format(sql.Identifier(self.env.schema)),
            (operation_id,)).fetchone()[0]

    def reasons(self, operation_id):
        return [r[0] for r in self.admin.execute(sql.SQL(
            "SELECT reason FROM {}.safeact_operation_transitions "
            "WHERE operation_id = %s ORDER BY seq"
        ).format(sql.Identifier(self.env.schema)), (operation_id,))]

    def wait_marker(self, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.marker.exists():
                point, pid = self.marker.read_text().split()
                return point, int(pid)
            time.sleep(0.05)
        self.fail("the worker never reached its failpoint")

    def wait_state(self, operation_id, state, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.state(operation_id)
            if current == state:
                return
            time.sleep(0.2)
        self.fail(f"{operation_id} stayed {current}, wanted {state}; "
                  f"{self.reasons(operation_id)}")

    def ledger_for(self, operation_id):
        key = self.admin.execute(sql.SQL(
            "SELECT provider_idempotency_key FROM {}.safeact_operations "
            "WHERE operation_id = %s"
        ).format(sql.Identifier(self.env.schema)),
            (operation_id,)).fetchone()[0]
        effects = self.ledger.get("/ledger/effects",
                                  params={"correlation": key}).json()
        calls = self.ledger.get("/ledger/calls",
                                params={"correlation": key}).json()
        return len(effects["effects"]), len(calls["calls"])

    def kill_at(self, point):
        self.worker(failpoint=point)
        operation_id = self.admit()
        reached, pid = self.wait_marker()
        self.assertEqual(reached, point)
        self.stop_worker()            # SIGKILL to the whole process group
        self.worker()                 # a fresh worker, no failpoint
        self.addCleanup(self.stop_worker)
        return operation_id, pid

    # -- cases -----------------------------------------------------------

    def test_1_killed_after_the_claim(self):
        operation_id, _ = self.kill_at("after_claim_commit")
        self.wait_state(operation_id, "SUCCEEDED")
        reasons = self.reasons(operation_id)
        self.assertIn("claim_abandoned_lease_expired", reasons)
        self.assertEqual(self.ledger_for(operation_id), (1, 1))

    def test_2_killed_after_the_provider_applied_it(self):
        operation_id, _ = self.kill_at("after_provider_call")
        self.wait_state(operation_id, "SUCCEEDED")
        reasons = self.reasons(operation_id)
        self.assertIn("lease_expired_investigate", reasons)
        self.assertIn("verify_with_provider", reasons)
        # Investigated, never dispatched a second time.
        self.assertEqual(self.ledger_for(operation_id), (1, 1))

    def test_3_killed_before_the_outcome_commit(self):
        operation_id, _ = self.kill_at("before_outcome_commit")
        self.wait_state(operation_id, "SUCCEEDED")
        self.assertIn("lease_expired_investigate", self.reasons(operation_id))
        self.assertEqual(self.ledger_for(operation_id), (1, 1))

    def test_4_a_prefork_child_dies_while_its_parent_lives(self):
        self.worker(failpoint="after_provider_call")
        self.addCleanup(self.stop_worker)
        parent = self.stack.processes["worker"]
        operation_id = self.admit()
        _, child = self.wait_marker()
        self.assertNotEqual(child, parent.pid)
        os.kill(child, signal.SIGKILL)
        time.sleep(1)
        self.assertIsNone(parent.poll(), "the pool parent must survive")
        # The replacement child is armed too, but the redelivered task
        # finds the work owned (DISPATCHING) and never reaches dispatch.
        self.wait_state(operation_id, "SUCCEEDED")
        self.assertEqual(self.ledger_for(operation_id), (1, 1))
        self.assertIsNone(parent.poll())
        self.assertIn("lease_expired_investigate", self.reasons(operation_id))
        # The pool saw its child die and did not acknowledge the task. Its
        # maintenance thread logs that asynchronously, so wait for the line
        # (a single read raced on the CI runner, PR #7, 2026-10-25).
        path = Path(self.tmp.name) / "logs" / "worker.log"
        deadline = time.monotonic() + 15
        while ("WorkerLostError" not in path.read_text(errors="replace")
               and time.monotonic() < deadline):
            time.sleep(0.2)
        self.assertIn("WorkerLostError", path.read_text(errors="replace"))


if __name__ == "__main__":
    unittest.main()
