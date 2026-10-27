"""Safe compensation (0010): F13 and A6.

The inverse of the charge is its own operation, admitted only after a
confirmed order failure, built from the durable charge record, verified
by its own lifecycle, and never erasing the forward history. Forward,
failed and compensation effects are counted separately from the mock
providers' harness ledger.
"""

import json
import os
import unittest

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from psycopg import sql

from connectors.crm import CrmConnector
from connectors.crm_mock import MockCrmProvider
from connectors.ledger import EffectsLedger
from connectors.payments import PaymentsConnector
from connectors.payments_mock import MockPaymentsProvider
from core.lifecycle import LifecycleState as S
from core.workflow import WorkflowStatus as W
from storage import durable_lifecycle as L
from storage.experimental_durable_execution import (
    ExecutionWorker,
    InMemoryTaskQueue,
    relay_once,
)
from storage.experimental_recovery_worker import Reconciler
from storage.experimental_workflow import (
    WorkflowAdvancer,
    WorkflowRequest,
    create_workflow,
)
from tests.integration.lifecycle_fixture import (
    DisposableLifecycleDatabase,
    commit,
)
from tests.integration.profiles_fixture import (
    CRM_FAST,
    PAYMENTS_EVENTUAL_FAST,
)
from tests.integration.timing import margin_for, wait_until_past


LEASE = 1
FEE = 500


def lost_ack(point, _connection):
    if point == "after_advance_commit":
        raise ConnectionError("synthetic lost acknowledgement")


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class CompensationTests(unittest.TestCase):

    def setUp(self):
        self.db = DisposableLifecycleDatabase()
        self.addCleanup(self.db.cleanup)
        self.db.start()
        self.schema = self.db.schema
        self.clock = self.db.connect(self.db.recoverer, autocommit=True)
        self.ledger = EffectsLedger()
        self.payments = MockPaymentsProvider(ledger=self.ledger)
        self.crm = MockCrmProvider(ledger=self.ledger)

    # -- helpers -------------------------------------------------------

    def connectors(self):
        return [PaymentsConnector(self.payments), CrmConnector(self.crm)]

    def run_workers(self):
        queue = InMemoryTaskQueue()
        relay_once(self.db.connect(self.db.worker_b), self.schema, queue)
        return ExecutionWorker(
            connect=lambda: self.db.connect(self.db.worker_a),
            schema=self.schema, connectors=self.connectors(),
            lease_seconds=LEASE,
        ).run_until_idle(queue)

    def advance(self, hook=None):
        return WorkflowAdvancer(
            connect=lambda: self.db.connect(self.db.advancer),
            schema=self.schema, fault_hook=hook,
        ).advance_once()

    def reconcile(self):
        return Reconciler(
            connect=lambda: self.db.connect(self.db.recoverer),
            schema=self.schema, connectors=self.connectors(),
        ).reconcile_once()

    def row(self, workflow_id):
        return self.db.admin.execute(sql.SQL(
            "SELECT status, charge_operation_id, charge_state, "
            "order_operation_id, order_state, compensation_operation_id, "
            "compensation_state FROM {} WHERE workflow_id = %s"
        ).format(self.db.table("safeact_workflow_status")),
            (workflow_id,),
        ).fetchone()

    def status(self, workflow_id):
        return W(self.row(workflow_id)[0])

    def failed_order(self, **changes):
        """Charge succeeds, then the CRM rejects the order for good."""

        created = create_workflow(self.db.engine, WorkflowRequest(
            tenant_id="synthetic-tenant", request_key="wf-1",
            actor_id="synthetic-agent", customer_id="cus_1",
            case_id="case_1", sku="sku-replacement", fee_minor=FEE,
            payments_account_id="mock-payments-account",
            crm_account_id="mock-crm-account", **changes,
        ))
        self.run_workers()
        self.advance()
        self.crm.reject_next = "rej_case_closed"
        self.run_workers()
        self.assertEqual(self.status(created.workflow_id), W.COMPENSATING)
        return created

    def effects(self, operation_id):
        key = self.db.provider_key(operation_id) or str(operation_id)
        return len(self.ledger.effects_for(key))

    # -- the happy compensation ---------------------------------------------

    def test_confirmed_order_failure_refunds_exactly_the_charge(self):
        created = self.failed_order()
        workflow_id = created.workflow_id

        ((_, step, refund_id),) = self.advance()
        self.assertEqual(step, "compensation")

        # The inverse is derived from the durable charge record.
        payload, action, key = self.db.admin.execute(sql.SQL(
            "SELECT payload_canonical, action_type, "
            "provider_idempotency_key FROM {} WHERE operation_id = %s"
        ).format(self.db.table("safeact_operations")),
            (refund_id,),
        ).fetchone()
        charge_reference = self.ledger.effects_for(
            self.db.provider_key(created.charge_operation_id)
        )[0]
        self.assertEqual(json.loads(payload), {
            "amount_minor": FEE, "charge_id": charge_reference,
        })
        self.assertEqual(payload, json.dumps(
            json.loads(payload), sort_keys=True, separators=(",", ":"),
        ))
        self.assertEqual(action, "refund_payment")
        self.assertTrue(key.startswith("sa1_"))
        self.assertNotEqual(
            key, self.db.provider_key(created.charge_operation_id)
        )

        self.run_workers()
        (status, charge, charge_state, order, order_state, comp,
         comp_state) = self.row(workflow_id)
        self.assertEqual(W(status), W.COMPENSATED_VERIFIED)
        # A6: every history stays visible; the inverse erased nothing.
        self.assertEqual(
            (charge_state, order_state, comp_state),
            ("SUCCEEDED", "FAILED_CONFIRMED", "SUCCEEDED"),
        )
        self.assertEqual(
            (self.effects(charge), self.effects(order), self.effects(comp)),
            (1, 0, 1),  # forward, failed forward, compensation
        )

    # -- preconditions ---------------------------------------------------

    def test_never_compensated_before_a_confirmed_order_failure(self):
        created = create_workflow(self.db.engine, WorkflowRequest(
            tenant_id="synthetic-tenant", request_key="wf-2",
            actor_id="synthetic-agent", customer_id="cus_1",
            case_id="case_1", sku="sku", fee_minor=FEE,
            payments_account_id="mock-payments-account",
            crm_account_id="mock-crm-account",
            order_contract=CRM_FAST.version,
        ))
        advancer = self.db.connect(self.db.advancer)

        def compensate():
            return commit(advancer, lambda: L.admit_workflow_step(
                advancer, self.schema, created.workflow_id, "compensation",
            ))

        with self.assertRaises(L.LifecycleRejected):
            compensate()  # charge not even done

        self.run_workers()
        self.advance()
        with self.assertRaises(L.LifecycleRejected):
            compensate()  # order still in flight

        # The order is ambiguous and escalates: it may exist, so the
        # charge is never refunded automatically.
        self.crm.drop_from_index = True
        self.crm.fail_after_effect = True
        self.run_workers()
        order_id = self.row(created.workflow_id)[3]
        status = commit(self.clock, lambda: L.investigation_status(
            self.clock, self.schema, order_id,
        ))
        wait_until_past(
            self.clock, status.investigation_deadline,
            margin_for(CRM_FAST.investigation_window),
        )
        self.reconcile()
        self.assertEqual(self.status(created.workflow_id), W.UNRESOLVED)
        with self.assertRaises(L.LifecycleRejected):
            compensate()
        self.assertEqual(self.advance(), [])
        self.assertEqual(self.db.count(
            "safeact_workflow_steps", workflow_id=created.workflow_id
        ), 2)

    # -- F04 / F11: exactly one inverse --------------------------------------

    def test_lost_ack_and_concurrency_admit_one_compensation(self):
        created = self.failed_order()
        ((_, _, first),) = self.advance(lost_ack)

        contenders = [self.db.connect(self.db.advancer) for _ in range(8)]
        barrier = Barrier(len(contenders))

        def compensate(connection):
            barrier.wait()
            return commit(connection, lambda: L.admit_workflow_step(
                connection, self.schema, created.workflow_id,
                "compensation",
            ))

        with ThreadPoolExecutor(len(contenders)) as pool:
            ids = set(pool.map(compensate, contenders))
        self.assertEqual(ids, {first})
        self.assertEqual(self.db.count(
            "safeact_workflow_steps", workflow_id=created.workflow_id
        ), 3)

        self.run_workers()
        self.run_workers()
        self.assertEqual(self.effects(first), 1)

    # -- F13: compensation times out or fails --------------------------------

    def test_f13_compensation_timeout_is_verified_not_assumed(self):
        created = self.failed_order()
        ((_, _, refund_id),) = self.advance()

        self.payments.fail_after_effect = True   # refund applied, no answer
        self.run_workers()
        self.assertEqual(self.status(created.workflow_id), W.COMPENSATING)

        self.reconcile()                         # strong lookup finds it
        self.assertEqual(
            self.status(created.workflow_id), W.COMPENSATED_VERIFIED
        )
        self.assertEqual(self.effects(refund_id), 1)

    def test_f13_unverifiable_compensation_stays_unresolved(self):
        created = self.failed_order(
            compensation_contract=PAYMENTS_EVENTUAL_FAST.version
        )
        self.payments.lookup_semantics = (
            PAYMENTS_EVENTUAL_FAST.contract.lookup_semantics
        )
        ((_, _, refund_id),) = self.advance()

        # The refund applies but stays invisible; its answer is lost.
        self.payments.hide_new_effects = True
        self.payments.fail_after_effect = True
        self.run_workers()

        status = commit(self.clock, lambda: L.investigation_status(
            self.clock, self.schema, refund_id,
        ))
        wait_until_past(
            self.clock, status.investigation_deadline,
            margin_for(PAYMENTS_EVENTUAL_FAST.investigation_window),
        )
        self.reconcile()
        # No restoration claim without applied evidence of the inverse.
        self.assertEqual(
            self.status(created.workflow_id), W.COMPENSATION_UNRESOLVED
        )
        self.assertEqual(self.row(created.workflow_id)[6],
                         "ESCALATED_UNRESOLVED")
        self.assertEqual(self.effects(refund_id), 1)

    def test_f13_rejected_compensation_is_unresolved_never_restored(self):
        created = self.failed_order()
        self.advance()
        self.payments.reject_next = "rej_refund_window_closed"
        self.run_workers()
        self.assertEqual(
            self.status(created.workflow_id), W.COMPENSATION_UNRESOLVED
        )
        self.assertEqual(
            self.row(created.workflow_id)[2], S.SUCCEEDED.value
        )


if __name__ == "__main__":
    unittest.main()
