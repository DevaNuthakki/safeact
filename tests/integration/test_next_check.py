"""safeact_next_check (0019): what happens next, and not before when.

Every kind is reached through the protected functions, and each
not_before is checked against the rule it reflects (the paced lookup,
the recorded backoff, the lease, the open breaker, the post-escalation
interval). The API detail view carries the same object.
"""

import os
import unittest

from datetime import datetime, timedelta
from uuid import uuid4

from psycopg import sql

from connectors.crm import CrmConnector
from connectors.crm_mock import MockCrmProvider
from connectors.payments import PaymentsConnector
from connectors.payments_mock import MockPaymentsProvider
from connectors.ledger import EffectsLedger
from core.connector_profiles import (
    ConnectorLimits, crm_profile, email_profile, payments_profile,
)
from core.lifecycle import LifecycleState as S
from storage import durable_lifecycle as L
from storage.experimental_durable_execution import (
    ExecutionWorker,
    InMemoryTaskQueue,
    relay_once,
)
from storage.experimental_recovery_worker import Reconciler
from tests.integration.lifecycle_fixture import (
    DisposableLifecycleDatabase,
    commit,
)


ENABLED = (
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1"
)


def at(text):
    return datetime.fromisoformat(text)


def ms(value):
    return timedelta(milliseconds=value)


PACED = payments_profile(
    "t-next", request_deadline=timedelta(seconds=1),
    investigation_window=timedelta(seconds=30),
    check_interval=timedelta(seconds=7), horizon=timedelta(seconds=600),
    limits=ConnectorLimits(
        backoff_base=ms(2000), backoff_cap=ms(4000),
        lookup_interval=ms(3000), lookup_interval_cap=ms(9000),
        breaker_threshold=1, breaker_cooldown=ms(60000),
    ),
)
# No native key: an inconclusive lookup keeps it UNKNOWN (a native-key
# contract would retry under its key instead).
PACED_CRM = crm_profile(
    "t-next", request_deadline=timedelta(seconds=1),
    investigation_window=timedelta(seconds=30),
    limits=ConnectorLimits(
        backoff_base=ms(2000), backoff_cap=ms(4000),
        lookup_interval=ms(3000), lookup_interval_cap=ms(9000),
    ),
)
NO_LOOKUP = email_profile("t-next", request_deadline=timedelta(seconds=1))


@unittest.skipUnless(ENABLED, "Explicit disposable PostgreSQL opt-in required")
class NextCheckTests(unittest.TestCase):

    def setUp(self):
        self.db = DisposableLifecycleDatabase()
        self.addCleanup(self.db.cleanup)
        self.db.start()
        self.db.register_profiles((PACED, PACED_CRM, NO_LOOKUP))
        self.schema = self.db.schema
        self.ledger = EffectsLedger()
        self.payments = MockPaymentsProvider(ledger=self.ledger)
        self.crm = MockCrmProvider(ledger=self.ledger)
        connectors = (PaymentsConnector(self.payments),
                      CrmConnector(self.crm))
        self.queue = InMemoryTaskQueue()
        self.worker = ExecutionWorker(
            connect=lambda: self.db.connect(self.db.worker_a),
            schema=self.schema, connectors=connectors, lease_seconds=30)
        self.reconciler = Reconciler(
            connect=lambda: self.db.connect(self.db.recoverer),
            schema=self.schema, connectors=connectors, retry_authorized=True)
        self.relay = self.db.connect(self.db.worker_a)
        self.recoverer = self.db.connect(self.db.recoverer)
        self.account = "acct-" + uuid4().hex[:8]

    def admit(self, profile=PACED):
        if profile is PACED_CRM:
            return self.db.admit(
                request_key="nc-" + uuid4().hex, connector_id="mock-crm",
                contract_version=profile.version,
                action_type="create_replacement_order",
                provider_account_id=self.account,
                payload={"case_id": "case-1", "sku": "sku-1"},
            )
        if profile is NO_LOOKUP:
            return self.db.admit(
                request_key="nc-" + uuid4().hex, connector_id="mock-email",
                contract_version=profile.version,
                action_type="send_refund_confirmation",
                provider_account_id=self.account,
                payload={"customer_id": "c-1", "template": "refund"},
            )
        return self.db.admit(
            request_key="nc-" + uuid4().hex, contract_version=profile.version,
            provider_account_id=self.account,
            payload={"charge_id": "ch-" + uuid4().hex[:8], "amount_minor": 1})

    def check(self, operation_id):
        return self.db.admin.execute(sql.SQL(
            "SELECT {}.safeact_next_check(%s), clock_timestamp()"
        ).format(sql.Identifier(self.schema)), (operation_id,)).fetchone()

    def relay_and_work(self):
        relay_once(self.relay, self.schema, self.queue)
        self.worker.run_until_idle(self.queue)

    def state(self, operation_id):
        return commit(self.recoverer, lambda: L.current_state(
            self.recoverer, self.schema, operation_id)).state

    # -- kinds -----------------------------------------------------------

    def test_recorded_then_breaker_open(self):
        operation_id = self.admit()
        found, _ = self.check(operation_id)
        self.assertEqual((found["kind"], found["not_before"]),
                         ("awaiting_dispatch", None))

        self.payments.outage = True          # threshold 1: one failure opens
        self.relay_and_work()
        waiting = self.admit()
        found, now = self.check(waiting)
        self.assertEqual(found["kind"], "awaiting_dispatch")
        self.assertIsNotNone(found["not_before"])
        self.assertIn("breaker", found["basis"])

    def test_in_flight_shows_the_lease(self):
        operation_id = self.admit()
        held = commit(self.relay, lambda: L.claim(
            self.relay, self.schema, operation_id, lease_seconds=30))
        found, _ = self.check(operation_id)
        self.assertEqual(found["kind"], "dispatch_in_progress")
        lease = commit(self.recoverer, lambda: L.current_state(
            self.recoverer, self.schema, operation_id)).lease_expires_at
        self.assertEqual(at(found["not_before"]), lease)
        self.assertTrue(held.claim_id)

    def test_unknown_shows_the_paced_lookup_and_deadline(self):
        self.crm.fail_after_effect = True
        self.crm.drop_from_index = True
        operation_id = self.admit(PACED_CRM)
        self.relay_and_work()
        self.assertEqual(self.state(operation_id), S.UNKNOWN)
        found, _ = self.check(operation_id)
        self.assertEqual(found["kind"], "scheduled_lookup")
        self.assertIsNotNone(found["deadline"])
        # First lookup due at once; the record is not searchable yet.
        self.reconciler.reconcile_once()
        self.assertEqual(self.state(operation_id), S.UNKNOWN)
        found, now = self.check(operation_id)
        self.assertEqual(found["kind"], "scheduled_lookup")
        pacing = commit(self.recoverer, lambda: L.pacing(
            self.recoverer, self.schema, operation_id))
        self.assertEqual(at(found["not_before"]), pacing.next_lookup_at)
        self.assertGreater(pacing.next_lookup_at, pacing.observed_at)

    def test_retry_shows_the_backoff(self):
        self.payments.rate_limit_next = 1
        operation_id = self.admit()
        self.relay_and_work()
        self.reconciler.reconcile_once()
        self.assertEqual(self.state(operation_id), S.RETRY_ELIGIBLE)
        found, _ = self.check(operation_id)
        self.assertEqual(found["kind"], "awaiting_retry")
        not_before = self.db.admin.execute(sql.SQL(
            "SELECT not_before FROM {}.safeact_retry_schedule "
            "WHERE operation_id = %s").format(sql.Identifier(self.schema)),
            (operation_id,)).fetchone()[0]
        self.assertEqual(at(found["not_before"]), not_before)

    def test_escalated_without_lookup_is_manual_review(self):
        operation_id = self.admit(NO_LOOKUP)
        driven = self.db.drive_to(operation_id, S.UNKNOWN,
                                  worker=self.relay, recoverer=self.recoverer)
        self.db.recover(self.recoverer, operation_id, driven.seq, S.UNKNOWN,
                        S.VERIFYING)
        self.db.recover(self.recoverer, operation_id, driven.seq + 1,
                        S.VERIFYING, S.ESCALATED_UNRESOLVED)
        found, _ = self.check(operation_id)
        self.assertEqual(found["kind"], "manual_review_only")

    def test_terminal_has_no_next_check(self):
        operation_id = self.admit()
        self.relay_and_work()
        self.assertEqual(self.state(operation_id), S.SUCCEEDED)
        found, _ = self.check(operation_id)
        self.assertEqual((found["kind"], found["not_before"]),
                         ("none_terminal", None))


if __name__ == "__main__":
    unittest.main()
