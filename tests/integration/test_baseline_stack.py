"""Owned B3 processes and post-commit/pre-publication mock registration."""

import os
import signal
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import redis
import sqlalchemy as sa

from chaos.baselines import registration as reg
from chaos.baselines.runtime import provider_key
from chaos.baselines.stack import Stack
from chaos.baselines.store import Request
from chaos.baselines.workflow import WorkflowRequest
from chaos.provider_trace import ObservedProvider
from chaos.quiescence import ProviderBarrier, process_group_exists
from chaos.workflow_faults import WorkflowPayments
from connectors.crm_mock import MockCrmProvider
from connectors.email_mock import MockEmailProvider
from connectors.http_mock.servers import MockProviderServers
from connectors.ledger import EffectsLedger
from storage.database import local_redis_url


ENABLED=all(os.environ.get(k)=='1' for k in ('SAFEACT_RUN_DB_INTEGRATION','SAFEACT_RUN_DISPOSABLE_DB_TESTS','SAFEACT_RUN_BROKER_TESTS'))


@unittest.skipUnless(ENABLED,'Actual disposable database/broker stack tests are opt-in')
class BaselineStackTests(unittest.TestCase):
    def setUp(self):
        self.broker=redis.Redis.from_url(local_redis_url(9));self.addCleanup(self.broker.close)
        mine=self.broker.client_id()
        self.assertFalse(any(int(c.get('db',0))==9 and int(c['id'])!=mine for c in self.broker.client_list()),'Reserved Redis DB 9 is in use')
        self.broker.flushdb();self.addCleanup(self.broker.flushdb)
        self.private=tempfile.TemporaryDirectory(prefix='safeact-b3-owned-');self.addCleanup(self.private.cleanup)
        self.ledger=EffectsLedger();self.payments=WorkflowPayments(ledger=self.ledger)
        self.crm=MockCrmProvider(ledger=self.ledger);self.email=MockEmailProvider(ledger=self.ledger)
        self.barrier=ProviderBarrier();mocks=dict(payments=self.payments,crm=self.crm,email=self.email)
        self.servers=MockProviderServers(ledger=self.ledger,stall_seconds=1.5,providers=tuple(
            ObservedProvider(k,v,self.ledger.record_observation,barrier=self.barrier) for k,v in mocks.items())).start()
        self.addCleanup(self.servers.stop)
        self.slot=SimpleNamespace(proxy_ports={k:self.servers.ports[k] for k in mocks},redis_url=local_redis_url(9),mocks=mocks,
            close_provider_admission=self.barrier.close,wait_provider_idle=self.barrier.wait_idle)
        self.stack=None;self.raw={};self.allow_integrity=False;self.releases=[]
        self.addCleanup(self.cleanup_stack)

    def start(self,components=('worker','relay','reconciler','advancer')):
        self.stack=Stack(self.slot,Path(self.private.name)/'stack',components=components)
        return self.stack.start()

    def cleanup_stack(self):
        for event in self.releases:event.set()
        if self.stack is None:return
        problems=[]
        try:
            if not self.stack._drained:
                try:self.stack.drain()
                except RuntimeError as error:problems.append(error)
            if self.stack.env.created:
                self.raw['final_operations']={k:asdict(v) for k,v in self.stack.snapshot().operations.items()}
                with self.stack.env.engine.connect() as c:
                    self.raw['committed_deliveries']=[dict(r) for r in c.execute(sa.select(self.stack.env.t.deliveries)).mappings()]
            self.raw.update(self.stack.observations());self.raw['ledger']=self.ledger.events()
            self.raw['integrity']=self.stack.integrity_errors();self.raw['declared_integrity_failure']=self.allow_integrity
        finally:
            if not self.stack._closed:
                try:self.stack.close()
                except RuntimeError as error:problems.append(error)
        if problems and not self.allow_integrity:raise problems[0]
        self.assertFalse(self.stack.env.created);self.assertFalse(self.stack.config_path.exists())
        self.assertTrue(all(g['process'].poll() is not None and not process_group_exists(g['process'].pid) for g in self.stack.generations))
        if self.stack.controller is not None:self.assertFalse(self.stack.controller.thread.is_alive())

    def request(self,connector='payments',version='q-v2-eventual'):
        target='target-'+uuid4().hex;amount=500
        return Request.from_payload(tenant_id='north',requester_id='requester',request_key='intent-'+uuid4().hex,
            connector_id='mock-'+connector,contract_version=version,provider_account_id='account-'+connector,
            action_type='refund_payment' if connector=='payments' else 'create_replacement_order',resource_id=target,
            payload=dict(charge_id=target,amount_minor=amount) if connector=='payments' else dict(case_id=target,sku='replacement'))

    def until(self,operation,state,timeout=20):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.stack.check_alive();sample=self.stack.snapshot()
            if sample.operations[str(operation)].state==state:return sample
            time.sleep(.05)
        self.fail('Owned stack did not reach '+state)

    def wait_worker_consuming(self,timeout=20):
        # The harness waits only for the API. A worker signalled before Celery has
        # installed its shutdown handlers dies of SIGTERM (-15) instead of exiting 0,
        # which quiescence rightly reports; Celery installs them before its consumer
        # blocks on BRPOP. A quiesce right after start reproduced -15 4/4 (2026-10-26).
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.stack.check_alive()
            if any(int(c.get('db',0))==9 and c.get('cmd')=='brpop' for c in self.broker.client_list()):return
            time.sleep(.05)
        self.fail('B3 worker never started consuming')

    def pause_registration(self):
        entered,release=threading.Event(),threading.Event();original=self.payments.register_key;self.releases.append(release)
        def paused(key,version):
            entered.set()
            if not release.wait(8):raise RuntimeError('Test registration barrier expired')
            original(key,version)
        self.payments.register_key=paused;return entered,release

    def test_registration_blocks_publication_without_holding_database_locks(self):
        entered,release=self.pause_registration();self.start(('worker','relay'))
        op=self.stack.submit(self.request()).operation_id
        try:
            self.assertTrue(entered.wait(5));self.assertEqual(self.broker.llen(self.stack.queue),0)
            self.assertEqual(self.ledger.events(),())
            with self.stack.env.engine.begin() as c:
                c.exec_driver_sql("SET LOCAL lock_timeout = '500ms'")
                c.execute(sa.select(self.stack.env.t.jobs).with_for_update()).all()
                c.execute(sa.select(self.stack.env.t.outbox).with_for_update()).all()
                self.assertGreater(c.scalar(sa.select(sa.func.count()).select_from(self.stack.env.t.deliveries)),0)
        finally:release.set()
        self.until(op,'SUCCEEDED')
        self.assertIn(provider_key(op),self.payments.eventual_keys)
        data=self.stack.observations();finished={e['request_id']:e for e in data['registration'] if e['event']=='registration_finished'}
        self.assertTrue(finished)
        first_publish=min(e['monotonic_ns'] for e in data['broker'] if e['event']=='publish_started')
        self.assertLess(min(e['monotonic_ns'] for e in finished.values()),first_publish)
        self.assertEqual(len(self.ledger.effects_for(provider_key(op))),1)

    def test_workflow_advancer_registers_charge_and_separate_refund_children(self):
        self.start();self.crm.reject_next='case-closed'
        self.payments.configure_workflow('control',self.stack.snapshot)
        request=WorkflowRequest(tenant_id='north',requester_id='requester',request_key='replacement-'+uuid4().hex,
            customer_id='customer',case_id='case',sku='replacement',fee_minor=500,payments_account_id='pay',crm_account_id='crm',
            charge_contract='q-v2-eventual',compensation_contract='q-v2-short')
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts=[f.result(timeout=10) for f in [pool.submit(self.stack.create_workflow,request) for _ in range(4)]]
        self.assertEqual(len({r.workflow_id for r in receipts}),1);self.assertEqual(sum(r.created for r in receipts),1)
        receipt=receipts[0];sample=self.until(receipt.workflow_id,'COMPENSATED_VERIFIED')
        root=next(e for e in reversed(sample.database) if e['event']=='db_workflow_sample');steps={s['step']:s for s in root['steps']}
        charge_key=provider_key(steps['charge']['operation_id']);inverse_key=provider_key(steps['compensation']['operation_id'])
        self.assertIn(charge_key,self.payments.eventual_keys);self.assertIn(inverse_key,self.payments.short_keys)
        charge_reference=self.ledger.effects_for(charge_key)[0]
        self.assertEqual(steps['compensation']['resource_id'],charge_reference)
        self.assertEqual(self.ledger.calls_for(inverse_key),({'charge_id':charge_reference,'amount_minor':500},))
        self.assertEqual(len(self.ledger.snapshot()[1]),2)

    def test_lost_publication_ack_replays_registration_and_preserves_intent(self):
        self.start(('worker','relay','reconciler'))
        armed=self.stack.trace_dir/'after_publication_commit';armed.write_text('lost_ack');armed.chmod(0o600)
        request=self.request();receipt=self.stack.submit(request);op=receipt.operation_id
        replay=self.stack.submit(request);self.assertEqual(op,replay.operation_id);self.assertFalse(replay.created)
        self.until(op,'SUCCEEDED');data=self.stack.observations()
        error=next(e for e in data['execution'] if e['event']=='hook_error' and e.get('stage')=='publication')
        self.assertFalse(any(e['commit_xid']==error['commit_xid'] for e in data['registration']))
        self.assertEqual(len(self.ledger.calls_for(provider_key(op))),1)
        with self.stack.env.engine.connect() as c:
            self.assertGreaterEqual(c.scalar(sa.select(sa.func.count()).select_from(self.stack.env.t.deliveries)),2)

    def test_failed_mock_registration_leaves_accepted_intent_accounted_without_publication(self):
        def refuse(*args):raise RuntimeError('Synthetic mock registration failure')
        self.payments.register_key=refuse;self.start(('worker','relay'));self.allow_integrity=True
        op=self.stack.submit(self.request()).operation_id;deadline=time.monotonic()+5
        while not self.stack.controller.errors and time.monotonic()<deadline:time.sleep(.02)
        self.assertTrue(self.stack.controller.errors)
        with self.assertRaises(RuntimeError):self.stack.check_alive()
        self.assertEqual(self.stack.snapshot().operations[str(op)].state,'RECORDED')
        self.assertEqual(self.broker.llen(self.stack.queue),0);self.assertEqual(self.ledger.events(),())

    def test_changed_registered_binding_refuses_a_later_batch(self):
        self.start(('worker','relay'));request=self.request(version='q-v2');op=self.stack.submit(request).operation_id
        self.until(op,'SUCCEEDED')
        with self.stack.env.engine.begin() as c:
            c.execute(self.stack.env.t.jobs.update().where(self.stack.env.t.jobs.c.operation_id==op).values(contract_version='q-v2-short'))
        self.allow_integrity=True;second=self.stack.submit(self.request()).operation_id;deadline=time.monotonic()+5
        while not self.stack.controller.errors and time.monotonic()<deadline:time.sleep(.02)
        self.assertTrue(self.stack.controller.errors)
        self.assertEqual(self.stack.snapshot().operations[str(second)].state,'RECORDED')
        self.assertEqual(len(self.ledger.snapshot()[1]),1);self.assertEqual(self.ledger.calls_for(provider_key(second)),())

    def test_shutdown_cancels_a_waiting_relay_without_publishing(self):
        entered,release=self.pause_registration();self.start(('worker','relay'));op=self.stack.submit(self.request()).operation_id
        self.assertTrue(entered.wait(5));relay=self.stack.processes['relay'];self.wait_worker_consuming()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.stack.quiesce);deadline=time.monotonic()+3
            try:
                while relay.poll() is None and time.monotonic()<deadline:time.sleep(.02)
                self.assertEqual(relay.poll(),0)
            finally:release.set()
            future.result(timeout=8)
        self.assertEqual(self.ledger.events(),());self.assertEqual(self.stack.snapshot().operations[str(op)].state,'RECORDED')
        self.assertFalse(any(e['event']=='registration_finished' for e in self.stack.observations()['registration']))

    def test_relay_restart_uses_a_fresh_generation_and_keeps_every_owned_pid(self):
        entered,release=self.pause_registration();self.start(('worker','relay'));op=self.stack.submit(self.request()).operation_id
        self.assertTrue(entered.wait(5));old=self.stack.processes['relay'];self.stack.kill('relay');new=self.stack.spawn('relay')
        release.set();self.until(op,'SUCCEEDED')
        self.assertNotEqual(old.pid,new.pid);self.assertIn(old,self.stack.processes.values())
        rows=self.stack.observations()['registration'];requests=[e for e in rows if e['event']=='registration_requested']
        self.assertTrue({old.pid,new.pid}<={e['pid'] for e in requests})
        self.assertEqual(len({e['request_id'] for e in requests}),len(requests))
        self.assertTrue(all(e['pid']!=old.pid for e in rows if e['event']=='registration_finished'))
        self.assertEqual(len(self.ledger.effects_for(provider_key(op))),1)

    def test_prefork_worker_restart_keeps_old_group_for_verified_cleanup(self):
        self.start(('worker','relay'));first=self.stack.submit(self.request()).operation_id;self.until(first,'SUCCEEDED')
        old=self.stack.processes['worker'];self.stack.kill('worker',signal.SIGKILL);new=self.stack.spawn('worker')
        second=self.stack.submit(self.request()).operation_id;self.until(second,'SUCCEEDED')
        self.assertNotEqual(old.pid,new.pid);self.assertIn(old,self.stack.processes.values())
        self.assertEqual(len(self.ledger.snapshot()[1]),2)

    def test_unrequested_clean_exit_cannot_be_hidden_by_a_replacement(self):
        self.start(('relay',));op=self.stack.submit(self.request()).operation_id
        deadline=time.monotonic()+5
        while not reg.messages(self.stack.registration_dir,self.stack.env.t.schema,'finished') and time.monotonic()<deadline:time.sleep(.02)
        self.assertTrue(reg.messages(self.stack.registration_dir,self.stack.env.t.schema,'finished'))
        old=self.stack.processes['relay'];self.allow_integrity=True
        os.killpg(old.pid,signal.SIGTERM);self.assertEqual(old.wait(timeout=5),0)
        self.stack.spawn('relay')
        self.assertIn('unexpected_exit_before_restart:relay:0',self.stack.integrity_errors())
        with self.assertRaises(RuntimeError):self.stack.check_alive()
        self.assertIn(old,self.stack.processes.values())
        self.assertEqual(self.stack.snapshot().operations[str(op)].state,'RECORDED');self.assertEqual(self.ledger.events(),())

    def test_held_late_effect_drains_after_execution_stops_before_schema_drop(self):
        self.start(('worker','relay'));held=self.stack.hold_next('crm',respond_now=True)
        op=self.stack.submit(self.request('crm','q-v2')).operation_id;self.assertTrue(held.started.wait(8))
        self.until(op,'UNKNOWN');self.assertEqual(self.ledger.effects_for(str(op)),())
        self.stack.quiesce();self.stack.drain();self.assertTrue(held.committed.is_set())
        self.assertTrue(self.stack.env.created);self.assertEqual(len(self.ledger.effects_for(str(op))),1)
        self.assertEqual(self.stack.snapshot().operations[str(op)].state,'UNKNOWN')
        self.assertLess(next(e['monotonic_ns'] for e in self.stack.events if e['event']=='b3_execution_stopped'),
                        next(e['monotonic_ns'] for e in self.ledger.events() if e['event']=='effect'))

    def test_owned_review_api_health_and_human_login(self):
        self.start(('worker','relay','reconciler','advancer','api'))
        self.stack.env.provision_human(actor_id='investigator',tenant_id='north',username='investigator',
            password='synthetic-stack-password',roles=('investigator',))
        with httpx2.Client(base_url=self.stack.base_url,trust_env=False) as client:
            self.assertEqual(client.get('/healthz').status_code,200)
            response=client.post('/login',json=dict(username='investigator',password='synthetic-stack-password'))
            self.assertEqual(response.status_code,200);self.assertTrue(response.json()['token'])

    def test_controller_death_is_retained_and_never_silently_bypassed(self):
        self.start(('worker','relay'));self.stack.controller.stop();self.allow_integrity=True
        with self.assertRaises(RuntimeError):self.stack.check_alive()
        op=self.stack.submit(self.request()).operation_id
        deadline=time.monotonic()+3
        while not reg.messages(self.stack.registration_dir,self.stack.env.t.schema,'request') and time.monotonic()<deadline:time.sleep(.02)
        self.assertTrue(reg.messages(self.stack.registration_dir,self.stack.env.t.schema,'request'))
        self.assertEqual(self.broker.llen(self.stack.queue),0);self.assertEqual(self.ledger.events(),())
        self.assertEqual(self.stack.snapshot().operations[str(op)].state,'RECORDED')

    def test_admission_rollback_cannot_register_or_publish_an_uncommitted_intent(self):
        self.start(('worker','relay'));request=self.request()
        def reject(c,cursor,statement,parameters,context,many):
            if statement.startswith('INSERT INTO') and '.baseline_outbox ' in statement:raise RuntimeError('Synthetic outbox rollback')
        sa.event.listen(self.stack.env.engine,'before_cursor_execute',reject)
        try:
            with self.assertRaises(RuntimeError):self.stack.submit(request)
        finally:sa.event.remove(self.stack.env.engine,'before_cursor_execute',reject)
        with self.stack.env.engine.connect() as c:
            self.assertEqual(c.scalar(sa.select(sa.func.count()).select_from(self.stack.env.t.jobs)),0)
        self.assertEqual(reg.messages(self.stack.registration_dir,self.stack.env.t.schema),[])
        op=self.stack.submit(request).operation_id;self.until(op,'SUCCEEDED')
        self.assertEqual(len(self.ledger.effects_for(provider_key(op))),1)
