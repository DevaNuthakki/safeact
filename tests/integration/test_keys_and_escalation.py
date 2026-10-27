"""C5b hardening (0008): derived provider keys, escalation of retry work
that can no longer be reclaimed, and evidence-triggered reopen.
"""

import os
import threading
import time
import unittest

from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql

from connectors.email import EmailConnector
from connectors.email_mock import MockEmailProvider
from connectors.ledger import EffectsLedger
from connectors.payments import PaymentsConnector
from connectors.payments_mock import MockPaymentsProvider
from core.lifecycle import LifecycleState as S
from core.provider_keys import derive_provider_key
from core.recovery import DecisionKind as D
from storage import durable_lifecycle as L
from storage.experimental_durable_execution import (
    ExecutionStatus as X,
    ExecutionTask,
    ExecutionWorker,
)
from storage.experimental_recovery_worker import Reconciler
from tests.integration.lifecycle_fixture import (
    DisposableLifecycleDatabase,
    commit,
)
from tests.integration.profiles_fixture import EMAIL_SLOW, PAYMENTS_FAST
from tests.integration.timing import margin_for, wait_until_past


LEASE = 1


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class KeysAndEscalationTests(unittest.TestCase):

    def setUp(self):
        self.db = DisposableLifecycleDatabase()
        self.addCleanup(self.db.cleanup)
        self.db.start()
        self.schema = self.db.schema
        self.clock = self.db.connect(self.db.recoverer, autocommit=True)
        self.ledger = EffectsLedger()
        self.payments = MockPaymentsProvider(ledger=self.ledger)
        self.email = MockEmailProvider(ledger=self.ledger)
        self.count = 0

    def admit(self, contract, **changes):
        self.count += 1
        return self.db.admit(
            request_key=f"op-{self.count}",
            contract_version=contract.version, **changes,
        )

    def connectors(self):
        return [PaymentsConnector(self.payments), EmailConnector(self.email)]

    def execute(self, operation_id):
        return ExecutionWorker(
            connect=lambda: self.db.connect(self.db.worker_a),
            schema=self.schema, connectors=self.connectors(),
            lease_seconds=LEASE,
        ).execute(ExecutionTask(operation_id, uuid4(), uuid4()))

    def reconcile(self):
        return Reconciler(
            connect=lambda: self.db.connect(self.db.recoverer),
            schema=self.schema, connectors=self.connectors(),
            retry_authorized=True,
        ).reconcile_once()

    def current(self, operation_id):
        return L.current_state(self.clock, self.schema, operation_id)

    def last_reason(self, operation_id):
        return self.db.admin.execute(sql.SQL(
            "SELECT reason FROM {} WHERE operation_id = %s "
            "ORDER BY seq DESC LIMIT 1"
        ).format(self.db.table("safeact_operation_transitions")),
            (operation_id,),
        ).fetchone()[0]

    # -- derived provider keys -------------------------------------------

    def test_stored_key_is_the_server_derivation(self):
        operation_id = self.admit(PAYMENTS_FAST)
        request = self.db.request(request_key="op-1")
        self.assertEqual(self.db.provider_key(operation_id), derive_provider_key(
            request.tenant_id, "op-1", request.connector_id,
            request.provider_account_id,
        ))

    def test_database_refuses_a_key_that_is_not_the_derivation(self):
        operation_id = self.admit(PAYMENTS_FAST)
        row = self.db.admin.execute(sql.SQL(
            "SELECT tenant_id, action_type, resource_id, "
            "provider_account_id, connector_id, contract_version, "
            "payload_sha256, payload_canonical FROM {} "
            "WHERE operation_id = %s"
        ).format(self.db.table("safeact_operations")),
            (operation_id,),
        ).fetchone()
        with self.assertRaisesRegex(
            psycopg.errors.CheckViolation, "provider_key_derived"
        ):
            with self.db.admin.transaction():
                self.db.admin.execute(sql.SQL(
                    "INSERT INTO {} (operation_id, request_key, tenant_id, "
                    "action_type, resource_id, provider_account_id, "
                    "connector_id, contract_version, payload_sha256, "
                    "payload_canonical, provider_idempotency_key) VALUES "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                ).format(self.db.table("safeact_operations")), (
                    uuid4(), "caller-chosen", *row, "caller-chosen-key",
                ))

    # -- unreclaimable retry work goes to a human -------------------------

    def test_abandoned_claim_limit_escalates_for_review(self):
        operation_id = self.admit(PAYMENTS_FAST)
        worker = self.db.connect(self.db.worker_a)
        for _ in range(3):
            held = commit(worker, lambda: L.claim(
                worker, self.schema, operation_id, lease_seconds=60,
            ))
            commit(worker, lambda: L.release_claim(
                worker, self.schema, held, reason="gave_up",
            ))
        self.assertIs(self.current(operation_id).state, S.RETRY_ELIGIBLE)

        (escalated,) = self.reconcile()
        self.assertIs(escalated.state, S.ESCALATED_UNRESOLVED)
        self.assertIs(
            self.current(operation_id).state, S.ESCALATED_UNRESOLVED
        )
        self.assertEqual(
            self.last_reason(operation_id), "abandoned_claim_limit"
        )
        self.assertEqual(self.ledger.snapshot(), ((), ()))

    def test_unregistered_contract_escalates_on_exhausted_budget(self):
        operation_id = self.admit(SimpleNamespace(version="v9"))
        self.assertEqual(self.execute(operation_id).stage, "fence")
        self.reconcile()
        self.assertIs(
            self.current(operation_id).state, S.ESCALATED_UNRESOLVED
        )
        self.assertEqual(
            self.last_reason(operation_id), "attempt_budget_exhausted"
        )

    def test_escalation_from_retry_eligible_needs_the_blocking_reason(self):
        operation_id = self.admit(PAYMENTS_FAST)
        driven = self.db.drive_to(
            operation_id, S.RETRY_ELIGIBLE,
            worker=self.db.connect(self.db.worker_a),
            recoverer=self.db.connect(self.db.recoverer),
        )
        recoverer = self.db.connect(self.db.recoverer)
        # Still reclaimable: escalation is refused whatever the reason.
        for reason in ("abandoned_claim_limit", "attempt_budget_exhausted",
                       "operator_preference"):
            with self.subTest(reason=reason):
                with self.assertRaises(L.IllegalTransition):
                    commit(recoverer, lambda: L.recovery_transition(
                        recoverer, self.schema, operation_id,
                        expected_seq=driven.seq,
                        expected_state=S.RETRY_ELIGIBLE,
                        to_state=S.ESCALATED_UNRESOLVED, reason=reason,
                    ))

    # -- evidence-triggered reopen (email) ---------------------------------

    def test_email_late_success_reopens_on_recorded_evidence_only(self):
        operation_id = self.admit(
            EMAIL_SLOW, connector_id="mock-email",
            action_type="send_refund_confirmation",
            resource_id="synthetic-customer",
            payload={"to": "customer@example.invalid", "template": "refund"},
        )
        held = self.email.hold_next()
        results = []
        original = threading.Thread(
            target=lambda: results.append(self.execute(operation_id))
        )
        original.start()
        deadline = time.monotonic() + 5
        while not self.ledger.calls_for(str(operation_id)):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        wait_until_past(
            self.clock, self.current(operation_id).lease_expires_at,
            margin_for(LEASE),
        )

        (escalated,) = self.reconcile()
        self.assertEqual(escalated.decision, D.ESCALATE_UNRESOLVED)

        # Nothing new recorded yet: no reopen, and no provider call.
        (waiting,) = self.reconcile()
        self.assertIs(
            self.current(operation_id).state, S.ESCALATED_UNRESOLVED
        )

        # The original's late reply is recorded after the escalation.
        held.release()
        original.join(5)
        self.assertEqual(results[0].status, X.LATE_EVIDENCE)

        (resolved,) = self.reconcile()
        self.assertEqual(resolved.decision, D.CONFIRM_APPLIED)
        self.assertIs(self.current(operation_id).state, S.SUCCEEDED)
        connection = self.db.connect(self.db.recoverer)
        status = commit(connection, lambda: L.investigation_status(
            connection, self.schema, operation_id,
        ))
        self.assertEqual(status.reopens, 1)
        self.assertEqual(len(self.ledger.calls_for(str(operation_id))), 1)
        self.assertEqual(len(self.ledger.effects_for(str(operation_id))), 1)


if __name__ == "__main__":
    unittest.main()
