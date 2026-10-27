"""Independent B3 snapshots scored against actual HTTP provider effects."""

import copy
import time
import unittest
from dataclasses import asdict, replace
from datetime import timedelta
from uuid import uuid4

import sqlalchemy as sa

from chaos import oracle
from chaos.baselines.observer import MissingBinding, Observer
from chaos.scenarios import generate
from tests.integration import test_baseline_workflow as fixture


@unittest.skipUnless(fixture.ENABLED,"Disposable PostgreSQL observer tests are opt-in")
class BaselineObserverTests(unittest.TestCase):
    setUp = fixture.BaselineWorkflowTests.setUp
    request = fixture.BaselineWorkflowTests.request
    rows = fixture.BaselineWorkflowTests.rows
    execute_ready = fixture.BaselineWorkflowTests.execute_ready
    settle = fixture.BaselineWorkflowTests.settle

    def observer(self):
        return Observer(self.env.engine,self.t)

    def create(self):
        receipt=self.workflow.create(self.request())
        return receipt,self.observer().sample(workflow_ids=[receipt.workflow_id])

    def test_native_request_is_retained_and_children_are_not_extra_admissions(self):
        receipt,observed=self.create()
        root=next(e for e in observed.database if e['event']=='db_workflow_sample')
        self.assertEqual(set(observed.operations),{str(receipt.workflow_id)})
        self.assertEqual(root['binding_format'],'b3-request-v1');self.assertNotIn('plan',root)
        self.assertEqual(root['request'],self.rows(self.t.workflows)[0]['request'])
        self.assertEqual(root['steps'][0]['resource_id'],root['request']['customer_id'])
        self.assertTrue(all(0<e['sample_started_ns']<=e['sample_finished_ns']==e['monotonic_ns']
                            for e in observed.database))
        audit=next(e for e in observed.database if e['event']=='db_audit_committed')
        self.assertGreater(int(audit['commit_xid']),0)

    def test_uncommitted_changes_and_rollback_are_invisible(self):
        receipt,_=self.create();op=receipt.charge_operation_id
        with self.env.engine.connect() as writer:
            transaction=writer.begin()
            writer.execute(self.t.jobs.update().where(self.t.jobs.c.operation_id==op).values(status='CLAIMED',version=1))
            observed=self.observer().sample(operation_ids=[op])
            self.assertEqual(observed.operations[str(op)].state,'RECORDED')
            transaction.rollback()
        self.assertEqual(self.observer().sample(operation_ids=[op]).operations[str(op)].state,'RECORDED')

    def test_writer_commits_during_sample_without_mixing_versions_or_blocking(self):
        receipt,_=self.create();tasks=[];self.runtime.relay_once(tasks.append)
        observer=self.observer();fired=[]
        def concurrent_commit(c,cursor,statement,parameters,context,executemany):
            if not fired and statement.startswith('SELECT ') and 'baseline_jobs' in statement:
                fired.append(True)
                self.assertIsNotNone(self.runtime.claim(tasks[0]))
        sa.event.listen(self.env.engine,'after_cursor_execute',concurrent_commit)
        try:observed=observer.sample(operation_ids=[receipt.charge_operation_id])
        finally:sa.event.remove(self.env.engine,'after_cursor_execute',concurrent_commit)
        self.assertTrue(fired)
        self.assertEqual(observed.operations[str(receipt.charge_operation_id)].state,'RECORDED')
        self.assertEqual([e['event_type'] for e in observed.database if e['event']=='db_audit_committed'],['INTENT_RECORDED'])
        fresh=observer.sample(operation_ids=[receipt.charge_operation_id])
        self.assertEqual(fresh.operations[str(receipt.charge_operation_id)].state,'CLAIMED')

    def test_snapshot_transaction_is_enforced_read_only(self):
        receipt,_=self.create()
        with self.assertRaises(sa.exc.DBAPIError):
            with self.observer().snapshot() as c:
                self.assertEqual(c.scalar(sa.text('SHOW transaction_read_only')),'on')
                self.assertEqual(c.scalar(sa.text('SHOW transaction_isolation')),'repeatable read')
                c.execute(self.t.jobs.update().where(self.t.jobs.c.operation_id==receipt.charge_operation_id)
                          .values(version=99))
        self.assertEqual(self.rows(self.t.jobs)[0]['version'],0)

    def test_missing_admitted_identities_and_original_binding_fail_observation(self):
        for arguments in (dict(operation_ids=[uuid4()]),dict(workflow_ids=[uuid4()])):
            with self.subTest(arguments=arguments),self.assertRaises(MissingBinding):self.observer().sample(**arguments)
        request=self.request();wid=uuid4()
        with self.env.engine.begin() as c:
            c.execute(self.t.workflows.insert().values(workflow_id=wid,tenant_id=request.tenant_id,
                requester_id=request.requester_id,request_key=request.request_key,request=asdict(request)))
        with self.assertRaises(MissingBinding):self.observer().sample(workflow_ids=[wid])

    def test_database_clock_proves_live_and_expired_dispatch_and_recovery_deadlines(self):
        receipt,_=self.create();tasks=[];self.runtime.relay_once(tasks.append)
        self.runtime.lease=timedelta(milliseconds=250)
        claim=self.runtime.claim(tasks[0]);fence=self.runtime.fence(claim)
        self.assertIsNotNone(fence)
        before=self.observer().sample(operation_ids=[receipt.charge_operation_id])
        lease=next(e for e in before.database if e['event']=='db_lease_sample')
        recovery=next(e for e in before.database if e['event']=='db_recovery_sample')
        self.assertTrue(lease['lease_active']);self.assertFalse(recovery['deadline_passed'])
        self.assertEqual(lease['attempt_id'],str(fence.attempt_id))
        self.assertAlmostEqual(recovery['investigation_deadline'],
            self.rows(self.t.attempts)[0]['fenced_at'].timestamp()+8)
        time.sleep(.3)
        after=self.observer().sample(operation_ids=[receipt.charge_operation_id])
        self.assertFalse(next(e for e in after.database if e['event']=='db_lease_sample')['lease_active'])
        # Cross the actual eight-second database deadline, with no clock edits.
        until=time.monotonic()+10
        while time.monotonic()<until:
            expired=self.observer().sample(operation_ids=[receipt.charge_operation_id])
            if next(e for e in expired.database if e['event']=='db_recovery_sample')['deadline_passed']:break
            time.sleep(.1)
        else:self.fail('Database investigation deadline did not expire within the observation budget')

    def workflow_case(self,mode):
        case=generate('dev','F13',0,7);spec=case.workflow
        case=replace(case,operations=(replace(case.operations[0],acceptable_terminal=('confirmed_success',
            'verified_compensation','escalated_unresolved')),))
        request=self.request(customer_id=spec.customer_id,case_id=spec.case_id,sku=spec.sku,
            fee_minor=case.operations[0].amount_minor,charge_contract=spec.charge_contract,
            order_contract=spec.order_contract,compensation_contract=spec.compensation_contract)
        start=time.time();started_ns=time.monotonic_ns();receipt=self.workflow.create(request)
        samples=[]
        def capture():samples.extend(self.observer().sample(workflow_ids=[receipt.workflow_id]).database)
        self.execute_ready()
        if mode!='normal':self.crm.reject_next='actual-order-rejection'
        self.workflow.advance_once();self.execute_ready()
        if mode!='normal':
            self.workflow.advance_once();capture()
            if mode!='control':self.payments.configure_workflow(mode,capture)
            self.execute_ready()
            if mode=='lost_response':self.settle(receipt.workflow_id,'COMPENSATED_VERIFIED')
            elif mode=='hidden_response':self.settle(receipt.workflow_id,'COMPENSATION_UNRESOLVED')
        observed=self.observer().sample(workflow_ids=[receipt.workflow_id]);samples.extend(observed.database)
        raw=dict(arm='b3',scenario_id=case.scenario_id,started=start,finished=time.time(),
            observation_finished_monotonic_ns=time.monotonic_ns(),
            admissions=[dict(index=0,copy=0,changed=False,local_id=str(receipt.workflow_id),refused=None)],
            operations={k:asdict(v) for k,v in observed.operations.items()},database=samples,
            ledger=self.ledger.events(),timeline=[dict(event='submission_started',index=0,changed=False,
                at=start,monotonic_ns=started_ns)],integrity=[],harness_errors=[])
        # Retain this whitelist in development evidence, after cleanup.
        self.raw,self.case=raw,case
        return oracle.evaluate(case,raw)

    def test_actual_normal_workflow_passes_the_common_oracle(self):
        row=self.workflow_case('normal')
        self.assertFalse(oracle.safety_failures(row),row);self.assertFalse(row['harness_errors'])
        self.assertTrue(row['operations'][0]['safe_completion']);self.assertEqual(row['forward_effects'],2)

    def test_actual_inverse_with_distinct_provider_accounts_passes_the_common_oracle(self):
        row=self.workflow_case('control')
        self.assertFalse(oracle.safety_failures(row),row);self.assertFalse(row['harness_errors'])
        self.assertTrue(row['operations'][0]['safe_completion']);self.assertEqual(row['compensation_effects'],1)
        root=[e for e in self.raw['database'] if e['event']=='db_workflow_sample'][-1]
        self.assertNotEqual(root['request']['payments_account_id'],root['request']['crm_account_id'])

    def test_actual_lost_inverse_reply_requires_matching_lookup_before_restoration(self):
        row=self.workflow_case('lost_response')
        self.assertFalse(oracle.safety_failures(row),row);self.assertTrue(row['operations'][0]['safe_completion'])
        self.assertEqual(row['compensation_effects'],1)

    def test_actual_hidden_inverse_stays_unresolved_and_preserves_effect(self):
        row=self.workflow_case('hidden_response')
        self.assertFalse(oracle.safety_failures(row),row);self.assertFalse(row['operations'][0]['safe_completion'])
        self.assertEqual(row['operations'][0]['outcome_class'],'escalated_unresolved')
        self.assertEqual(row['compensation_effects'],1)

    def test_actual_rejected_inverse_does_not_claim_restoration(self):
        row=self.workflow_case('rejected')
        self.assertFalse(oracle.safety_failures(row),row);self.assertEqual(row['compensation_effects'],0)
        self.assertEqual(row['operations'][0]['outcome_class'],'pending')
        self.assertFalse(row['operations'][0]['safe_completion'])

    def test_corrupt_native_binding_cannot_hide_behind_consistent_provider_effects(self):
        self.workflow_case('control')
        for field in ('tenant_id','requester_id','request_key','payments_account_id','crm_account_id',
                      'customer_id','case_id','sku','fee_minor','charge_contract','order_contract','compensation_contract'):
            raw=copy.deepcopy(self.raw)
            for sample in raw['database']:
                if sample['event']=='db_workflow_sample':sample['request'][field]='changed'
            with self.subTest(field=field):self.assertTrue(oracle.safety_failures(oracle.evaluate(self.case,raw)))
        for field in ('resource_id','provider_account_id','request_key','requester_id','tenant_id','compensates_operation_id'):
            raw=copy.deepcopy(self.raw)
            for sample in raw['database']:
                if sample['event']=='db_workflow_sample':
                    next(s for s in sample['steps'] if s['step']=='compensation')[field]='unrelated'
            with self.subTest(child_field=field):self.assertTrue(oracle.safety_failures(oracle.evaluate(self.case,raw)))
