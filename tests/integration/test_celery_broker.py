"""F08 against a real Celery + Redis broker (local compose Redis).

The broker is coordination only: messages can be lost, duplicated or
redelivered, and every admitted operation still executes at most once
and is accounted for, because the claim in PostgreSQL decides and the
relay republishes from the durable outbox.

Uses Redis database 15 only, and flushes only that database. The
worker runs in-process so the harness ledger stays readable.
"""

import os
import subprocess
import sys
import time
import unittest

from uuid import uuid4

import redis

from celery.contrib.testing.worker import start_worker

from connectors.ledger import EffectsLedger
from connectors.payments import PaymentsConnector
from connectors.payments_mock import MockPaymentsProvider
from core.lifecycle import LifecycleState as S
from storage import durable_lifecycle as L
from storage.database import local_redis_url
from storage.experimental_celery import CeleryPublisher, make_app
from storage.experimental_durable_execution import ExecutionWorker, relay_once
from storage.experimental_recovery_worker import account_operations
from tests.integration.lifecycle_fixture import DisposableLifecycleDatabase


BROKER_DB = 15
VISIBILITY = 3  # seconds; tasks here finish well within it

# A consumer that takes one message without acknowledging it and dies
# without any cleanup: the broker must hand the message out again.
DYING_CONSUMER = """
import os, sys
from kombu import Connection, Exchange, Queue
url, name = os.environ["SAFEACT_BROKER_URL"], sys.argv[1]
with Connection(url, transport_options={"visibility_timeout": %d}) as c:
    queue = Queue(name, Exchange(name), routing_key=name)(c.default_channel)
    queue.declare()
    message = queue.get(no_ack=False)
    print("took" if message is not None else "empty", flush=True)
    os._exit(9)
""" % VISIBILITY


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1"
    and os.environ.get("SAFEACT_RUN_BROKER_TESTS") == "1",
    "Explicit PostgreSQL and Redis broker opt-in required",
)
class CeleryBrokerTests(unittest.TestCase):

    def setUp(self):
        self.url = local_redis_url(BROKER_DB)
        self.redis = redis.Redis.from_url(self.url)
        self.redis.flushdb()
        self.addCleanup(self.redis.flushdb)

        self.db = DisposableLifecycleDatabase()
        self.addCleanup(self.db.cleanup)
        self.db.start()
        self.schema = self.db.schema

        self.queue = "safeact-it-" + uuid4().hex[:12]
        self.app = make_app(
            self.url, queue=self.queue, visibility_timeout=VISIBILITY
        )
        self.ledger = EffectsLedger()
        self.provider = MockPaymentsProvider(ledger=self.ledger)
        self.app.safeact_execute = ExecutionWorker(
            connect=lambda: self.db.connect(self.db.worker_a),
            schema=self.schema,
            connectors=[PaymentsConnector(self.provider)],
            lease_seconds=30,
        ).execute
        self.publisher = CeleryPublisher(self.app, queue=self.queue)
        self.admitted = []
        self.addCleanup(self.assert_invariants)

    # -- helpers -------------------------------------------------------

    def admit(self, count=1):
        for _ in range(count):
            self.admitted.append(self.db.admit(
                request_key=f"broker-{len(self.admitted) + 1}"
            ))

    def relay(self):
        return relay_once(
            self.db.connect(self.db.worker_b), self.schema, self.publisher
        )

    def worker(self):
        return start_worker(
            self.app, pool="solo", concurrency=1,
            perform_ping_check=False, shutdown_timeout=10,
            loglevel="WARNING",
        )

    def states(self):
        return {
            operation_id: L.current_state(
                self.db.admin, self.schema, operation_id
            ).state
            for operation_id in self.admitted
        }

    def wait_for(self, state, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if set(self.states().values()) == {state}:
                return
            time.sleep(0.2)
        self.fail(f"not all {state.value}: {self.states()}")

    def effects(self, operation_id):
        return len(self.ledger.effects_for(self.db.provider_key(operation_id)))

    def assert_invariants(self):
        accounting = account_operations(
            self.db.connect(self.db.recoverer), self.schema
        )
        self.assertEqual(accounting.total, len(self.admitted))
        for operation_id in self.admitted:
            self.assertLessEqual(self.effects(operation_id), 1)

    # -- F08 ----------------------------------------------------------------

    def test_relay_through_redis_executes_each_operation_once(self):
        self.admit(3)
        self.assertEqual(len(self.relay()), 3)
        with self.worker():
            self.wait_for(S.SUCCEEDED)
        for operation_id in self.admitted:
            self.assertEqual(self.effects(operation_id), 1)

    def test_duplicate_publishes_have_one_claimant(self):
        self.admit(2)
        # The relay republishes never-claimed work on every pass.
        self.relay()
        self.relay()
        self.assertEqual(self.publisher.published, 4)
        with self.worker():
            self.wait_for(S.SUCCEEDED)
            time.sleep(1)  # let the duplicates reach the claim too
        for operation_id in self.admitted:
            self.assertEqual(self.effects(operation_id), 1)
            self.assertEqual(self.db.count(
                "safeact_operation_transitions",
                operation_id=operation_id, to_state="CLAIMED",
            ), 1)

    def test_redis_loss_never_loses_an_admitted_operation(self):
        self.admit(2)
        self.relay()
        self.assertTrue(self.redis.dbsize() > 0)

        # The broker loses every message (crash, flush, no AOF).
        self.redis.flushdb()
        with self.worker():
            time.sleep(1.5)
            self.assertEqual(set(self.states().values()), {S.RECORDED})

            # The durable outbox is the source: the next relay pass
            # republishes, and each operation runs once.
            self.relay()
            self.wait_for(S.SUCCEEDED)
        for operation_id in self.admitted:
            self.assertEqual(self.effects(operation_id), 1)

    def test_unacknowledged_message_is_redelivered_after_consumer_death(self):
        self.admit(1)
        self.relay()

        taken = subprocess.run(
            [sys.executable, "-c", DYING_CONSUMER, self.queue],
            env={**os.environ, "SAFEACT_BROKER_URL": self.url},
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(taken.stdout.strip(), "took")
        self.assertEqual(taken.returncode, 9)
        self.assertEqual(set(self.states().values()), {S.RECORDED})

        # Redis hands the unacked message out again once it is older
        # than the visibility timeout. kombu restores such messages when
        # a consumer starts, and otherwise only about every 100 s (every
        # 10th check of a 10 s cycle): start clearly after the timeout.
        time.sleep(VISIBILITY + 1.5)
        with self.worker():
            self.wait_for(S.SUCCEEDED, timeout=VISIBILITY + 30)
        self.assertEqual(self.effects(self.admitted[0]), 1)

    def test_settings_match_the_documented_contract(self):
        conf = self.app.conf
        self.assertTrue(conf.task_acks_late)
        self.assertTrue(conf.task_reject_on_worker_lost)
        self.assertEqual(conf.worker_prefetch_multiplier, 1)
        self.assertEqual(
            conf.broker_transport_options["visibility_timeout"], VISIBILITY
        )
        self.assertEqual(conf.accept_content, ["json"])


if __name__ == "__main__":
    unittest.main()
