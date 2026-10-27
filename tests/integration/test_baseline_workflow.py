"""B3's own workflow transactions and actual forward/inverse HTTP effects."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa

from chaos.baselines.environment import Environment
from chaos.baselines.processes import Config
from chaos.baselines.runtime import provider_key
from chaos.baselines.store import Conflict
from chaos.baselines.workflow import CorruptWorkflow, Workflow, WorkflowRequest
from chaos.provider_trace import ObservedProvider
from chaos.workflow_faults import WorkflowPayments
from connectors.crm_mock import MockCrmProvider
from connectors.email_mock import MockEmailProvider
from connectors.http_mock.servers import MockProviderServers
from connectors.ledger import EffectsLedger


ENABLED = (os.environ.get("SAFEACT_RUN_DB_INTEGRATION")=="1" and
           os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS")=="1")


@unittest.skipUnless(ENABLED,"Disposable PostgreSQL workflow tests are opt-in")
class BaselineWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.env = Environment().start();self.addCleanup(self.env.stop)
        self.t = self.env.t
        self.ledger = EffectsLedger()
        self.payments = WorkflowPayments(ledger=self.ledger)
        self.crm = MockCrmProvider(ledger=self.ledger)
        self.email = MockEmailProvider(ledger=self.ledger)
        providers = tuple(ObservedProvider(k,p,self.ledger.record_observation)
                         for k,p in (("payments",self.payments),("crm",self.crm),("email",self.email)))
        self.servers = MockProviderServers(ledger=self.ledger,providers=providers,stall_seconds=1.5).start()
        self.addCleanup(self.servers.stop)
        self.private = tempfile.TemporaryDirectory(prefix="safeact-b3-workflow-");self.addCleanup(self.private.cleanup)
        self.config = Path(self.private.name)/"private.json";self.env.write_config(self.config,self.servers.urls)
        self.context = Config(self.config).runtime();self.runtime = self.context.__enter__()
        self.addCleanup(self.context.__exit__,None,None,None)
        self.workflow = Workflow(self.runtime)

    def request(self,**changes):
        values = dict(tenant_id="north",requester_id="requester",request_key="replacement-"+uuid4().hex,
            customer_id="customer-"+uuid4().hex,case_id="case-"+uuid4().hex,sku="replacement",fee_minor=500,
            payments_account_id="payments-account",crm_account_id="crm-account")
        values.update(changes);return WorkflowRequest(**values)

    def rows(self,table):
        with self.env.engine.connect() as c:return [dict(r) for r in c.execute(sa.select(table)).mappings()]

    def execute_ready(self):
        tasks=[];self.runtime.relay_once(tasks.append)
        for task in tasks:
            row=next(r for r in self.rows(self.t.jobs) if r["operation_id"]==task.operation_id)
            if row["connector_id"]=="mock-payments":self.payments.register_key(provider_key(task.operation_id),row["contract_version"])
            self.runtime.execute(task)
        return tasks

    def create_charge(self,request=None):
        receipt=self.workflow.create(request or self.request());self.execute_ready()
        self.assertEqual(self.workflow.inspect(receipt.workflow_id)["steps"]["charge"]["status"],"SUCCEEDED")
        return receipt

    def create_inverse(self,mode="control"):
        receipt=self.create_charge()
        self.crm.reject_next="actual-order-rejection"
        self.assertIsNotNone(self.workflow.advance(receipt.workflow_id));self.execute_ready()
        self.assertIsNotNone(self.workflow.advance(receipt.workflow_id))
        if mode!="control":self.payments.configure_workflow(mode,lambda:None)
        self.execute_ready()
        return receipt

    def settle(self,workflow_id,expected,timeout=12):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.runtime.reconcile_once();self.workflow.advance_once();self.execute_ready()
            view=self.workflow.inspect(workflow_id)
            if view["status"]==expected:return view
            time.sleep(.05)
        self.fail("B3 workflow did not reach "+expected)

    def test_normal_workflow_has_two_distinct_forward_effects_and_no_inverse(self):
        receipt=self.create_charge();self.workflow.advance_once();self.execute_ready()
        view=self.workflow.inspect(receipt.workflow_id)
        self.assertEqual(view["status"],"COMPLETED")
        self.assertEqual(set(view["steps"]),{"charge","order"})
        self.assertEqual(len({s["operation_id"] for s in view["steps"].values()}),2)
        self.assertEqual(len(self.ledger.snapshot()[1]),2)
        self.assertEqual(self.workflow.advance_once(),{})

    def test_actual_order_rejection_refunds_exact_original_charge_and_amount(self):
        receipt=self.create_inverse();view=self.workflow.inspect(receipt.workflow_id)
        self.assertEqual(view["status"],"COMPENSATED_VERIFIED")
        steps=view["steps"];self.assertEqual(len({s["operation_id"] for s in steps.values()}),3)
        charge_key=provider_key(steps["charge"]["operation_id"])
        inverse_key=provider_key(steps["compensation"]["operation_id"])
        charge_reference=self.ledger.effects_for(charge_key)[0]
        self.assertEqual(self.ledger.calls_for(inverse_key),({"charge_id":charge_reference,"amount_minor":500},))
        self.assertEqual(steps["compensation"]["resource_id"],charge_reference)
        link=next(s for s in self.rows(self.t.steps) if s["step"]=="compensation")
        self.assertEqual(link["compensates_operation_id"],steps["charge"]["operation_id"])
        self.assertEqual(len(self.ledger.snapshot()[1]),2)

    def test_lost_refund_reply_is_verified_without_a_second_effect(self):
        receipt=self.create_inverse("lost_response")
        initial=self.workflow.inspect(receipt.workflow_id)
        self.assertEqual(initial["status"],"COMPENSATING")
        view=self.settle(receipt.workflow_id,"COMPENSATED_VERIFIED")
        key=provider_key(view["steps"]["compensation"]["operation_id"])
        self.assertEqual(len(self.ledger.effects_for(key)),1)
        self.assertEqual(len(self.ledger.calls_for(key)),1)

    def test_hidden_refund_effect_remains_unresolved_without_false_restoration(self):
        receipt=self.create_inverse("hidden_response")
        view=self.settle(receipt.workflow_id,"COMPENSATION_UNRESOLVED")
        inverse=view["steps"]["compensation"]
        self.assertEqual(inverse["status"],"ESCALATED_UNRESOLVED")
        self.assertEqual(len(self.ledger.effects_for(provider_key(inverse["operation_id"]))),1)
        self.assertLessEqual(len(self.ledger.calls_for(provider_key(inverse["operation_id"]))),3)

    def test_rejected_refund_keeps_forward_history_and_unresolved_compensation(self):
        receipt=self.create_inverse("rejected");view=self.workflow.inspect(receipt.workflow_id)
        self.assertEqual(view["status"],"COMPENSATION_UNRESOLVED")
        self.assertEqual(view["steps"]["charge"]["status"],"SUCCEEDED")
        self.assertEqual(view["steps"]["order"]["status"],"FAILED_CONFIRMED")
        self.assertEqual(view["steps"]["compensation"]["status"],"FAILED_CONFIRMED")
        self.assertEqual(len(self.ledger.snapshot()[1]),1)

    def test_uncertain_order_never_admits_a_refund(self):
        receipt=self.create_charge();self.crm.fail_after_effect=True;self.crm.drop_from_index=True
        self.workflow.advance_once();self.execute_ready()
        view=self.settle(receipt.workflow_id,"UNRESOLVED")
        self.assertNotIn("compensation",view["steps"])
        self.assertEqual(len(self.ledger.snapshot()[1]),2)
        self.assertEqual(len(self.rows(self.t.jobs)),2)

    def test_rejected_charge_admits_no_order_or_refund(self):
        self.payments.reject_next="customer-declined"
        receipt=self.workflow.create(self.request());self.execute_ready()
        self.assertIsNone(self.workflow.advance(receipt.workflow_id))
        self.assertEqual(self.workflow.inspect(receipt.workflow_id)["status"],"FAILED_NO_EFFECT")
        self.assertEqual(len(self.rows(self.t.jobs)),1)
        self.assertEqual(self.ledger.snapshot()[1],())

    def test_uncertain_charge_does_not_admit_an_order_until_actual_positive_lookup(self):
        self.payments.fail_after_effect=True;self.payments.hide_new_effects=True
        receipt=self.workflow.create(self.request(charge_contract="q-v2-eventual"));self.execute_ready()
        self.assertEqual(self.workflow.inspect(receipt.workflow_id)["steps"]["charge"]["status"],"UNKNOWN")
        self.assertIsNone(self.workflow.advance(receipt.workflow_id));self.assertEqual(len(self.rows(self.t.steps)),1)
        self.payments.reveal_hidden_effects();self.runtime.reconcile_once()
        self.assertEqual(self.workflow.inspect(receipt.workflow_id)["steps"]["charge"]["status"],"SUCCEEDED")
        self.assertIsNotNone(self.workflow.advance(receipt.workflow_id));self.execute_ready()
        self.assertEqual(self.workflow.inspect(receipt.workflow_id)["status"],"COMPLETED")
        self.assertEqual(len(self.ledger.snapshot()[1]),2)

    def test_complete_request_replay_survives_progress_but_rejects_every_change(self):
        request=self.request();receipt=self.create_charge(request)
        self.workflow.advance_once();self.execute_ready()
        replay=self.workflow.create(request)
        self.assertFalse(replay.created);self.assertEqual(replay.workflow_id,receipt.workflow_id)
        for field,value in (("requester_id","another-human"),("customer_id","other-customer"),("case_id","other-case"),
                            ("fee_minor",501),("sku","different"),("payments_account_id","other-account"),
                            ("crm_account_id","other-crm"),("charge_contract","q-v2-short"),
                            ("order_contract","q-v2-inflight"),("compensation_contract","q-v2-eventual")):
            with self.subTest(field=field),self.assertRaises(Conflict):self.workflow.create(replace(request,**{field:value}))
        self.assertEqual(len(self.rows(self.t.workflows)),1)
        self.assertEqual(len(self.rows(self.t.jobs)),2)

    def test_six_concurrent_creations_commit_one_complete_initial_bundle(self):
        request=self.request();barrier=threading.Barrier(6,timeout=10)
        def create():barrier.wait();return self.workflow.create(request)
        with ThreadPoolExecutor(max_workers=6) as pool:
            receipts=[f.result(timeout=15) for f in [pool.submit(create) for _ in range(6)]]
        self.assertEqual(sum(r.created for r in receipts),1)
        self.assertEqual(len({r.workflow_id for r in receipts}),1)
        for table in (self.t.workflows,self.t.steps,self.t.jobs,self.t.audit,self.t.outbox,self.t.workflow_audit):
            self.assertEqual(len(self.rows(table)),1)

    def test_lost_workflow_commit_acknowledgement_recovers_by_original_identity(self):
        request=self.request()
        def lose(point,details):
            if point=="after_workflow_admission_commit":raise ConnectionError("test workflow acknowledgement lost")
        self.runtime.hook=lose
        with self.assertRaises(ConnectionError):self.workflow.create(request)
        self.runtime.hook=lambda *_:None
        self.assertFalse(self.workflow.create(request).created)
        self.assertEqual(len(self.rows(self.t.jobs)),1)

    def test_creation_outbox_error_rolls_back_workflow_step_and_intent(self):
        def refuse(c,cursor,statement,parameters,context,many):
            if statement.lstrip().startswith("INSERT INTO") and ".baseline_outbox " in statement:raise RuntimeError("publication failure")
        sa.event.listen(self.runtime.engine,"before_cursor_execute",refuse)
        try:
            with self.assertRaisesRegex(RuntimeError,"publication failure"):self.workflow.create(self.request())
        finally:sa.event.remove(self.runtime.engine,"before_cursor_execute",refuse)
        for table in (self.t.workflows,self.t.steps,self.t.jobs,self.t.audit,self.t.outbox,self.t.workflow_audit):
            self.assertEqual(self.rows(table),[])

    def test_six_concurrent_advancers_admit_one_next_step(self):
        receipt=self.create_charge();barrier=threading.Barrier(6,timeout=10)
        def advance():barrier.wait();return self.workflow.advance(receipt.workflow_id)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results=[f.result(timeout=15) for f in [pool.submit(advance) for _ in range(6)]]
        self.assertEqual(sum(r is not None for r in results),1)
        self.assertEqual(len(self.rows(self.t.steps)),2)
        self.execute_ready();self.assertEqual(len(self.ledger.snapshot()[1]),2)

    def test_lost_advance_ack_replay_keeps_one_inverse(self):
        receipt=self.create_charge();self.crm.reject_next="case-closed"
        self.workflow.advance(receipt.workflow_id);self.execute_ready()
        def lose(point,details):
            if point=="after_workflow_advance_commit":raise ConnectionError("advance acknowledgement lost")
        self.runtime.hook=lose
        with self.assertRaises(ConnectionError):self.workflow.advance(receipt.workflow_id)
        self.runtime.hook=lambda *_:None
        self.assertIsNone(self.workflow.advance(receipt.workflow_id));self.execute_ready()
        self.assertEqual(len(self.rows(self.t.steps)),3)
        self.assertEqual(self.workflow.inspect(receipt.workflow_id)["status"],"COMPENSATED_VERIFIED")

    def test_advance_commit_failure_rolls_back_child_and_binding(self):
        receipt=self.create_charge()
        def refuse(point,details):
            if point=="before_workflow_advance_commit":raise RuntimeError("advance rollback")
        self.runtime.hook=refuse
        with self.assertRaisesRegex(RuntimeError,"advance rollback"):self.workflow.advance(receipt.workflow_id)
        for table in (self.t.steps,self.t.jobs,self.t.outbox):self.assertEqual(len(self.rows(table)),1)
        self.runtime.hook=lambda *_:None
        self.assertIsNotNone(self.workflow.advance(receipt.workflow_id))

    def test_actual_charge_and_refund_are_visible_only_after_binding_commit(self):
        observed=[]
        def check(point,details):
            if point!="before_provider_call":return
            with self.env.engine.connect() as c:
                binding=c.execute(sa.select(self.t.steps).where(
                    self.t.steps.c.operation_id==details["operation_id"])).mappings().one()
                observed.append(binding["step"])
                if binding["step"]=="compensation":
                    jobs={r["action_type"]:r for r in c.execute(sa.select(self.t.jobs)).mappings()}
                    self.assertEqual(jobs["charge_shipping_fee"]["status"],"SUCCEEDED")
                    self.assertEqual(jobs["create_replacement_order"]["status"],"FAILED_CONFIRMED")
        self.runtime.hook=check
        self.create_inverse()
        self.assertEqual(observed,["charge","order","compensation"])

    def test_wrong_inverse_binding_is_refused_without_provider_calls(self):
        receipt=self.create_inverse();inverse=self.workflow.inspect(receipt.workflow_id)["steps"]["compensation"]
        before=len(self.ledger.snapshot()[0])
        # A malicious/corrupt ordinary application row is a negative fixture,
        # not fabricated provider proof or a qualification outcome.
        wrong_amount=json.dumps(dict(charge_id=inverse["resource_id"],amount_minor=501),sort_keys=True,separators=(",",":"))
        variants=(dict(resource_id="wrong-original-charge"),dict(provider_account_id="wrong-account"),
                  dict(payload_canonical=wrong_amount,payload_sha256=sha256(wrong_amount.encode()).hexdigest()))
        for change in variants:
            with self.subTest(fields=tuple(change)):
                with self.env.engine.begin() as c:
                    c.execute(self.t.jobs.update().where(self.t.jobs.c.operation_id==inverse["operation_id"]).values(**change))
                with self.assertRaises(CorruptWorkflow):self.workflow.inspect(receipt.workflow_id)
                with self.assertRaises(CorruptWorkflow):self.workflow.advance(receipt.workflow_id)
                with self.env.engine.begin() as c:
                    c.execute(self.t.jobs.update().where(self.t.jobs.c.operation_id==inverse["operation_id"])
                              .values(**{k:inverse[k] for k in change}))
        self.assertEqual(len(self.ledger.snapshot()[0]),before)

    def test_real_advancer_process_admits_only_after_actual_charge_completion(self):
        receipt=self.create_charge()
        process=subprocess.Popen([sys.executable,"-m","chaos.baselines.processes","--config",str(self.config),"advancer"],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        try:
            deadline=time.monotonic()+10
            while time.monotonic()<deadline and len(self.rows(self.t.steps))<2:
                self.assertIsNone(process.poll());time.sleep(.05)
            self.assertEqual(len(self.rows(self.t.steps)),2)
            self.execute_ready();self.assertEqual(self.workflow.inspect(receipt.workflow_id)["status"],"COMPLETED")
        finally:
            if process.poll() is None:os.killpg(process.pid,signal.SIGTERM)
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5);self.fail("Advancer failed to stop")
        self.assertEqual(process.returncode,0)


if __name__=="__main__":unittest.main()
