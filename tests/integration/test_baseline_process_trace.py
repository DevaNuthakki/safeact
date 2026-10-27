"""Real B3 HTTP, PostgreSQL, SIGKILL and prefork broker observation joins."""

import copy
import json
import os
import signal
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa

from chaos.baselines.observer import Observer
from chaos.baselines.processes import Config, LostAcknowledgement
from chaos.baselines import trace
from chaos.baselines.workflow import Workflow, WorkflowRequest
from chaos.baselines.runtime import provider_key
from chaos.coverage import observed
from connectors.http_trace import correlation_hash
from tests.integration import test_baseline_runtime as fixture


@unittest.skipUnless(fixture.ENABLED,'Disposable PostgreSQL process trace checks are opt-in')
class BaselineProcessTraceTests(unittest.TestCase):
    submit=fixture.BaselineRuntimeTests.submit
    row=fixture.BaselineRuntimeTests.row
    task=fixture.BaselineRuntimeTests.task
    until=fixture.BaselineRuntimeTests.until
    wait_expired=fixture.BaselineRuntimeTests.wait_expired

    def setUp(self):
        fixture.BaselineRuntimeTests.setUp(self)
        self.traces=Path(self.directory.name)/'traces';self.traces.mkdir(mode=0o700)
        self.path=Path(self.directory.name)/'traced-config.json'
        self.env.write_config(self.path,self.servers.urls,fault_dir=self.traces)
        self.traced=Config(self.path).runtime();self.runtime=self.traced.__enter__()
        self.addCleanup(self.traced.__exit__,None,None,None)
        self.raw=dict(admissions=[],operations={},database=[],timeline=[],ledger=[],http=[],execution=[],broker=[])
        self.spawned=[]
        self.addCleanup(self.capture_final)

    def spawn(self,component,**kwargs):
        process,path=fixture.BaselineRuntimeTests.spawn(self,component,**kwargs)
        self.spawned.append((component,process));return process,path

    def records(self,kind):return trace.read_trace(self.traces,self.t.schema,kind)

    def admit(self,connector='crm'):
        op=self.submit(connector)
        self.raw['admissions'].append(dict(index=len(self.raw['admissions']),local_id=str(op),changed=False))
        self.capture(op);return op

    def capture(self,op):
        sample=Observer(self.env.engine,self.t).sample(operation_ids=[op])
        self.raw['database'].extend(sample.database)
        self.raw['operations'][str(op)]=asdict(sample.operations[str(op)])
        return sample.operations[str(op)]

    def capture_final(self):
        for a in self.raw['admissions']:self.capture(a['local_id'])
        for kind in ('http','execution','broker'):self.raw[kind]=self.records(kind)
        self.raw['ledger']=self.ledger.events()
        self.raw['observation_finished_monotonic_ns']=time.monotonic_ns()
        self.raw['observed_coverage']=sorted(observed(self.raw))
        self.raw['processes']=[dict(component=component,pid=p.pid,returncode=p.poll()) for component,p in self.spawned]
        self.assertTrue(all(p['returncode']==(-9 if p['component']=='execute' else 0) for p in self.raw['processes']))
        self.raw['forbidden_trace_markers']=[]
        for path in Path(self.directory.name).glob('*.log'):
            value=path.read_text()
            for marker in ('BASELINE_TRACE_ERROR','SAFEACT_HTTP_TRACE_ERROR'):
                if marker in value:self.raw['forbidden_trace_markers'].append(marker)
        self.assertFalse(self.raw['forbidden_trace_markers'])

    def snapshot_raw(self):
        for kind in ('http','execution','broker'):self.raw[kind]=self.records(kind)
        self.raw['ledger']=self.ledger.events();self.raw['observation_finished_monotonic_ns']=time.monotonic_ns()
        return self.raw

    def test_actual_http_calls_follow_committed_fences_for_all_connectors(self):
        for connector in ('crm','payments','email'):
            op=self.admit(connector);self.assertEqual(self.runtime.execute(self.task()),'recorded')
            self.assertEqual(self.capture(op).state,'SUCCEEDED')
            key=correlation_hash(provider_key(op) if connector=='payments' else str(op))
            starts=[e for e in self.records('http') if e['event']=='http_start' and e['correlation_sha256']==key]
            self.assertEqual(len(starts),1);http=starts[0]
            fence=next(e for e in self.records('execution') if e['event']=='commit_returned'
                       and e['stage']=='fence' and e.get('operation_id')==str(op))
            self.assertLess(fence['monotonic_ns'],http['monotonic_ns'])
            self.assertTrue(any(e['event']=='db_audit_committed' and e['commit_xid']==fence['commit_xid']
                                for e in self.raw['database']))
            provider=next(e for e in self.ledger.events() if e['event']=='provider_start'
                          and e.get('correlation_sha256')==key)
            response=next(e for e in self.records('http') if e['event']=='http_response' and e['request_id']==http['request_id'])
            self.assertLess(http['monotonic_ns'],provider['monotonic_ns']);self.assertLess(provider['monotonic_ns'],response['monotonic_ns'])

    def test_actual_lost_outcome_ack_joins_committed_audit_and_replay_without_resend(self):
        op=self.admit();task=self.task()
        armed=self.traces/'after_outcome_commit';armed.write_text('lost_ack');armed.chmod(0o600)
        with self.assertRaises(LostAcknowledgement):self.runtime.execute(task)
        self.assertEqual(self.capture(op).state,'SUCCEEDED')
        self.assertEqual(self.runtime.execute(task),'not_claimable')
        raw=self.snapshot_raw();self.assertIn('commit_ack_lost',observed(raw))
        self.assertEqual(len(self.ledger.calls_for(str(op))),1);self.assertEqual(len(self.ledger.effects_for(str(op))),1)
        error=next(e for e in raw['execution'] if e['event']=='hook_error')
        self.assertEqual(error['error_type'],'LostAcknowledgement')
        for key in ('execution','database'):
            changed=copy.deepcopy(raw);changed[key]=[]
            with self.subTest(missing=key):self.assertNotIn('commit_ack_lost',observed(changed))

    def test_actual_lost_fence_ack_has_committed_attempt_but_no_http_send(self):
        op=self.admit();task=self.task()
        armed=self.traces/'after_fence_commit';armed.write_text('lost_ack');armed.chmod(0o600)
        with self.assertRaises(LostAcknowledgement):self.runtime.execute(task)
        self.assertEqual(self.capture(op).state,'DISPATCHING')
        self.assertEqual(self.records('http'),[]);self.assertEqual(self.ledger.events(),())
        self.assertIn('commit_ack_lost',observed(self.snapshot_raw()))

    def test_actual_sigkill_after_http_effect_then_recovery_is_credited_without_resend(self):
        op=self.admit();task=self.task()
        armed=self.traces/'after_provider_call';armed.write_text('park');armed.chmod(0o600)
        process,path=self.spawn('execute',task=task)
        until=time.monotonic()+10
        while not (self.traces/'parked').exists() and process.poll() is None and time.monotonic()<until:time.sleep(.02)
        self.assertTrue((self.traces/'parked').exists(),path.read_text())
        marker=json.loads((self.traces/'parked').read_text());self.assertEqual(marker['pid'],process.pid)
        self.assertEqual(self.capture(op).state,'DISPATCHING')
        details=dict(worker_pid=process.pid,parked_pid=marker['pid'],process_group=process.pid,point=marker['point'],signal=9)
        self.raw['timeline'].append(dict(event='worker_kill_requested',monotonic_ns=time.monotonic_ns(),**details))
        os.killpg(process.pid,signal.SIGKILL);code=process.wait(timeout=5)
        self.raw['timeline'].append(dict(event='worker_killed',monotonic_ns=time.monotonic_ns(),returncode=code,**details))
        self.assertEqual(code,-9);self.wait_expired(op);self.until(op,'SUCCEEDED');self.capture(op)
        raw=self.snapshot_raw();self.assertIn('worker_crashed_after_send',observed(raw))
        self.assertEqual(len(self.ledger.calls_for(str(op))),1);self.assertEqual(len(self.ledger.effects_for(str(op))),1)
        changed=copy.deepcopy(raw);changed['timeline'][-1]['returncode']=0
        self.assertNotIn('worker_crashed_after_send',observed(changed))

    def test_separate_reconciler_records_matching_http_lookup_and_committed_result(self):
        op=self.admit();self.crm.fail_after_effect=True;self.runtime.execute(self.task())
        process,path=self.spawn('reconciler');until=time.monotonic()+10
        while time.monotonic()<until and process.poll() is None:
            if self.row(op)['status']=='SUCCEEDED' and any(e['event']=='commit_returned' and e.get('stage')=='lookup'
                    and e['pid']==process.pid for e in self.records('execution')):break
            time.sleep(.05)
        else:self.fail('Observed reconciler did not finish: '+path.read_text())
        self.assertEqual(self.capture(op).state,'SUCCEEDED')
        self.assertTrue(any(e['event']=='http_response' and e['method']=='GET' and e['pid']==process.pid for e in self.records('http')))
        self.assertEqual(len(self.ledger.calls_for(str(op))),1)

    @unittest.skipUnless(os.environ.get('SAFEACT_RUN_BROKER_TESTS')=='1','Actual Redis/prefork checks are opt-in')
    def test_real_prefork_delivery_signals_bind_worker_http_to_committed_delivery(self):
        import redis
        from storage.database import local_redis_url
        broker=redis.Redis.from_url(local_redis_url(9));self.addCleanup(broker.close)
        mine=broker.client_id()
        self.assertFalse(any(int(c.get('db',0))==9 and int(c['id'])!=mine for c in broker.client_list()),'Reserved test DB 9 is already used')
        broker.flushdb();self.addCleanup(broker.flushdb)
        op=self.admit();queue='baseline-'+uuid4().hex;config=Path(self.directory.name)/'traced-broker.json'
        self.env.write_config(config,self.servers.urls,fault_dir=self.traces,broker_url=local_redis_url(9),queue=queue)
        relay,relay_log=self.spawn('relay',config=config);until=time.monotonic()+10
        while broker.llen(queue)==0 and relay.poll() is None and time.monotonic()<until:time.sleep(.05)
        envelope=broker.lindex(queue,0);self.assertIsNotNone(envelope,relay_log.read_text())
        queued=trace.queued_message(envelope,queue)
        self.assertEqual(queued['operation_id'],str(op));self.raw['queued_identity']=queued
        worker,worker_log=self.spawn('worker',config=config);until=time.monotonic()+25
        while time.monotonic()<until and all(p.poll() is None for p in (relay,worker)):
            rows=self.records('broker')
            if self.row(op)['status']=='SUCCEEDED' and any(e['event']=='task_returned' and e.get('task_status')=='recorded' for e in rows):break
            time.sleep(.05)
        else:self.fail('Observed prefork task did not finish: '+relay_log.read_text()+worker_log.read_text())
        self.assertEqual(self.capture(op).state,'SUCCEEDED');rows=self.records('broker')
        started=next(e for e in rows if e['event']=='task_started' and e['operation_id']==str(op))
        returned=next(e for e in rows if e['event']=='task_returned' and e.get('task_status')=='recorded')
        received=next(e for e in rows if e['event']=='task_received' and e['task_id']==started['task_id'])
        self.assertEqual(received['pid'],worker.pid);self.assertNotEqual(started['pid'],worker.pid)
        self.assertEqual(started['pid'],returned['pid'])
        with self.env.engine.connect() as c:
            deliveries=[{k:str(v) for k,v in dict(r).items() if k in trace.IDS} for r in c.execute(sa.select(self.t.deliveries)).mappings()]
        self.raw['committed_deliveries']=deliveries
        self.assertTrue(any(all(d[k]==started[k] for k in trace.IDS[:3]) for d in deliveries))
        self.assertTrue(any(e['event']=='http_start' and e['method']=='POST' and e['pid']==started['pid'] for e in self.records('http')))
        self.assertEqual(len(self.ledger.calls_for(str(op))),1)

    def test_workflow_commit_observations_keep_native_workflow_identity(self):
        workflow=Workflow(self.runtime)
        request=WorkflowRequest(tenant_id='north',requester_id='requester',request_key='workflow-'+uuid4().hex,
            customer_id='customer',case_id='case',sku='replacement',fee_minor=500,
            payments_account_id='payments-account',crm_account_id='crm-account')
        receipt=workflow.create(request);op=receipt.charge_operation_id
        self.payments.register_key(provider_key(op),'q-v2')
        self.runtime.execute(self.task());workflow.advance(receipt.workflow_id);self.runtime.execute(self.task())
        self.assertEqual(workflow.inspect(receipt.workflow_id)['status'],'COMPLETED')
        rows=[e for e in self.records('execution') if e.get('workflow_id')==str(receipt.workflow_id)]
        self.assertEqual({e['stage'] for e in rows},{'workflow_admission','workflow_advance'})
        with self.env.engine.connect() as c:
            audits=[dict(r) for r in c.execute(sa.select(self.t.workflow_audit)).mappings()]
        self.raw['workflow_audit']=[dict(commit_xid=a['commit_xid'],workflow_id=str(a['workflow_id']),kind=a['kind']) for a in audits]
        self.assertTrue(all(any(e['event']=='commit_returned' and e['commit_xid']==a['commit_xid'] for e in rows) for a in audits))
