"""The fixed two-step workflow (0009): charge, then replacement order.

Each step is its own operation with its own lifecycle. The order is
admitted only after the charge SUCCEEDED, from the plan recorded at
creation, exactly once even under concurrent or ack-lost advancement.
Effects are counted from the mock providers' harness ledger.
"""

import os
import unittest

from concurrent.futures import ThreadPoolExecutor
from itertools import product
from threading import Barrier
from uuid import uuid4

import psycopg
from psycopg import sql

from connectors.crm import CrmConnector
from connectors.crm_mock import MockCrmProvider
from connectors.ledger import EffectsLedger
from connectors.payments import PaymentsConnector
from connectors.payments_mock import MockPaymentsProvider
from core.contracts import canonical_payload_bytes
from core.lifecycle import LifecycleState as S
from core.workflow import WorkflowStatus as W, workflow_status
from storage import durable_lifecycle as L
from storage.experimental_durable_execution import (
    InMemoryTaskQueue,
    ExecutionWorker,
    relay_once,
)
from storage.experimental_recovery_worker import Reconciler
from storage.experimental_workflow import (
    WorkflowAdvancer,
    WorkflowConflict,
    WorkflowRequest,
    create_workflow,
)
from tests.integration.lifecycle_fixture import (
    DisposableLifecycleDatabase,
    commit,
)
from tests.integration.profiles_fixture import CRM_FAST
from tests.integration.timing import margin_for, wait_until_past


LEASE = 1


def lost_ack(point, _connection):
    if point == "after_advance_commit":
        raise ConnectionError("synthetic lost acknowledgement")


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class WorkflowTests(unittest.TestCase):

    def setUp(self):
        self.db = DisposableLifecycleDatabase()
        self.addCleanup(self.db.cleanup)
        self.db.start()
        self.schema = self.db.schema
        self.clock = self.db.connect(self.db.recoverer, autocommit=True)
        self.ledger = EffectsLedger()
        self.payments = MockPaymentsProvider(ledger=self.ledger)
        self.crm = MockCrmProvider(ledger=self.ledger)
        self.addCleanup(self.assert_one_effect_per_operation)

    # -- helpers -------------------------------------------------------

    def request(self, key="wf-1", **changes):
        fields = dict(
            tenant_id="synthetic-tenant", request_key=key,
            actor_id="synthetic-agent", customer_id="cus_1",
            case_id="case_1", sku="sku-replacement", fee_minor=500,
            payments_account_id="mock-payments-account",
            crm_account_id="mock-crm-account",
        )
        fields.update(changes)
        return WorkflowRequest(**fields)

    def create(self, key="wf-1", **changes):
        return create_workflow(self.db.engine, self.request(key, **changes))

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

    def advancer(self, hook=None):
        return WorkflowAdvancer(
            connect=lambda: self.db.connect(self.db.advancer),
            schema=self.schema, fault_hook=hook,
        )

    def reconcile(self):
        return Reconciler(
            connect=lambda: self.db.connect(self.db.recoverer),
            schema=self.schema, connectors=self.connectors(),
            retry_authorized=False,
        ).reconcile_once()

    def status(self, workflow_id):
        row = self.db.admin.execute(sql.SQL(
            "SELECT status, order_operation_id FROM {} "
            "WHERE workflow_id = %s"
        ).format(self.db.table("safeact_workflow_status")),
            (workflow_id,),
        ).fetchone()
        return W(row[0]), row[1]

    def operation(self, operation_id):
        return self.db.admin.execute(sql.SQL(
            "SELECT connector_id, action_type, request_key, "
            "payload_canonical, provider_idempotency_key FROM {} "
            "WHERE operation_id = %s"
        ).format(self.db.table("safeact_operations")),
            (operation_id,),
        ).fetchone()

    def correlation(self, operation_id):
        return self.db.provider_key(operation_id) or str(operation_id)

    def steps(self, workflow_id):
        return self.db.count("safeact_workflow_steps", workflow_id=workflow_id)

    def assert_one_effect_per_operation(self):
        for (operation_id,) in self.db.admin.execute(sql.SQL(
            "SELECT operation_id FROM {}"
        ).format(self.db.table("safeact_operations"))).fetchall():
            self.assertLessEqual(
                len(self.ledger.effects_for(self.correlation(operation_id))),
                1,
            )

    # -- happy path and preconditions -------------------------------------

    def test_charge_then_order_completes(self):
        created = self.create()
        self.assertTrue(created.created)
        self.assertEqual(self.status(created.workflow_id)[0], W.IN_PROGRESS)
        self.assertEqual(self.advancer().advance_once(), [])

        self.run_workers()
        ((workflow_id, step, order_id),) = self.advancer().advance_once()
        self.assertEqual((workflow_id, step), (created.workflow_id, "order"))

        connector, action, request_key, payload, key = self.operation(
            order_id
        )
        self.assertEqual((connector, action),
                         ("mock-crm", "create_replacement_order"))
        self.assertEqual(request_key, f"wf:{workflow_id}:order")
        self.assertEqual(payload, canonical_payload_bytes(
            {"case_id": "case_1", "sku": "sku-replacement"}
        ).decode())
        self.assertIsNone(key)  # CRM has no native key
        self.assertTrue(self.db.provider_key(
            created.charge_operation_id
        ).startswith("sa1_"))

        self.run_workers()
        self.assertEqual(self.status(workflow_id)[0], W.COMPLETED)
        self.assertEqual(len(self.ledger.effects_for(
            self.correlation(created.charge_operation_id)
        )), 1)
        self.assertEqual(len(self.ledger.effects_for(str(order_id))), 1)

    def test_order_waits_for_a_successful_charge(self):
        created = self.create()
        advancer = self.db.connect(self.db.advancer)
        with self.assertRaises(L.WorkflowPrecondition):
            commit(advancer, lambda: L.admit_workflow_step(
                advancer, self.schema, created.workflow_id, "order",
            ))
        # Asking for an existing step only returns it (idempotent).
        self.assertEqual(commit(advancer, lambda: L.admit_workflow_step(
            advancer, self.schema, created.workflow_id, "charge",
        )), created.charge_operation_id)
        with self.assertRaises(L.WorkflowPrecondition):
            commit(advancer, lambda: L.admit_workflow_step(
                advancer, self.schema, created.workflow_id, "refund_all",
            ))
        self.assertEqual(self.steps(created.workflow_id), 1)

    def test_failed_charge_ends_without_effect(self):
        created = self.create()
        self.payments.reject_next = "rej_card"
        self.run_workers()
        self.assertEqual(self.advancer().advance_once(), [])
        self.assertEqual(
            self.status(created.workflow_id)[0], W.FAILED_NO_EFFECT
        )
        self.assertEqual(self.ledger.snapshot()[1], ())

    def test_unknown_charge_never_starts_the_order(self):
        created = self.create()
        self.payments.fail_before_effect = True
        self.run_workers()  # ambiguous: UNKNOWN
        self.assertEqual(self.advancer().advance_once(), [])
        self.assertEqual(self.status(created.workflow_id)[0], W.IN_PROGRESS)
        self.assertEqual(self.steps(created.workflow_id), 1)

    def test_escalated_order_is_unresolved_never_compensated(self):
        created = self.create(order_contract=CRM_FAST.version)
        self.run_workers()
        ((workflow_id, _, order_id),) = self.advancer().advance_once()
        self.crm.drop_from_index = True
        self.crm.fail_after_effect = True
        self.run_workers()

        connection = self.db.connect(self.db.recoverer)
        deadline = commit(connection, lambda: L.investigation_status(
            connection, self.schema, order_id,
        )).investigation_deadline
        wait_until_past(
            self.clock, deadline, margin_for(CRM_FAST.investigation_window)
        )
        self.reconcile()

        self.assertEqual(self.status(workflow_id)[0], W.UNRESOLVED)
        self.assertEqual(self.advancer().advance_once(), [])
        self.assertEqual(self.steps(workflow_id), 2)

    # -- F04 / F11 ----------------------------------------------------------

    def test_create_is_idempotent_and_refuses_a_changed_plan(self):
        first = self.create()
        again = self.create()
        self.assertEqual(
            (again.workflow_id, again.charge_operation_id, again.created),
            (first.workflow_id, first.charge_operation_id, False),
        )
        with self.assertRaises(WorkflowConflict):
            self.create(sku="sku-other")

    def test_concurrent_creates_make_one_workflow(self):
        barrier = Barrier(8)

        def create(_):
            barrier.wait()
            return self.create("wf-race").workflow_id

        with ThreadPoolExecutor(8) as pool:
            ids = set(pool.map(create, range(8)))
        self.assertEqual(len(ids), 1)
        self.assertEqual(self.db.count("safeact_workflows"), 1)
        self.assertEqual(self.db.count("safeact_operations"), 1)

    def test_replay_refuses_every_changed_charge_input(self):
        first = self.create()
        for changes in ({"fee_minor": 501}, {"customer_id": "another-customer"},
                        {"charge_contract": "v1-eventual"},
                        {"payments_account_id": "another-account"}):
            with self.subTest(changes=changes), self.assertRaises(WorkflowConflict):
                self.create(**changes)
        self.assertEqual(self.create().charge_operation_id, first.charge_operation_id)
        self.assertEqual(self.db.count("safeact_operations"), 1)

    def test_replay_after_dispatch_still_compares_original_charge(self):
        first = self.create()
        self.run_workers()
        self.advancer().advance_once()
        self.assertEqual(self.create().workflow_id, first.workflow_id)
        with self.assertRaises(WorkflowConflict):
            self.create(fee_minor=501)

    def test_concurrent_distinct_fees_preserve_one_original_charge(self):
        barrier = Barrier(2)

        def create(fee):
            barrier.wait(timeout=10)
            try:
                return self.create("wf-fee-race", fee_minor=fee)
            except WorkflowConflict:
                return None

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(create, (500, 501)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.db.count("safeact_workflows"), 1)
        self.assertEqual(self.db.count("safeact_operations"), 1)

    def test_lost_advance_ack_admits_the_order_once(self):
        created = self.create()
        self.run_workers()

        ((_, _, order_id),) = self.advancer(lost_ack).advance_once()
        self.assertIsNotNone(order_id)
        self.assertEqual(self.steps(created.workflow_id), 2)
        # A later pass finds nothing left to do.
        self.assertEqual(self.advancer().advance_once(), [])
        self.assertEqual(self.status(created.workflow_id)[1], order_id)

    def test_concurrent_advancers_admit_one_order(self):
        created = self.create()
        self.run_workers()
        contenders = [self.db.connect(self.db.advancer) for _ in range(8)]
        barrier = Barrier(len(contenders))

        def advance(connection):
            barrier.wait()
            return commit(connection, lambda: L.admit_workflow_step(
                connection, self.schema, created.workflow_id, "order",
            ))

        with ThreadPoolExecutor(len(contenders)) as pool:
            ids = set(pool.map(advance, contenders))
        self.assertEqual(len(ids), 1)
        self.assertEqual(self.steps(created.workflow_id), 2)

    # -- roles and the derived status -------------------------------------

    def test_only_the_advancer_role_admits_steps(self):
        created = self.create()
        for role in (self.db.worker_a, self.db.recoverer, self.db.stranger):
            connection = self.db.connect(role)
            with self.subTest(role=role):
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    L.admit_workflow_step(
                        connection, self.schema, created.workflow_id, "order"
                    )

    def test_sql_status_equals_the_python_model_for_every_combination(self):
        states = [None] + [state.value for state in S]
        for charge, order, compensation in product(states, repeat=3):
            sql_status = self.db.admin.execute(sql.SQL(
                "SELECT {}.safeact_workflow_status_of(%s, %s, %s)"
            ).format(sql.Identifier(self.schema)),
                (charge, order, compensation),
            ).fetchone()[0]
            with self.subTest(states=(charge, order, compensation)):
                if charge is None:
                    self.assertEqual(sql_status, "INVALID")
                    continue
                try:
                    expected = workflow_status(charge, order, compensation)
                except ValueError:
                    self.assertEqual(sql_status, "INVALID")
                else:
                    self.assertEqual(sql_status, expected.value)


if __name__ == "__main__":
    unittest.main()
