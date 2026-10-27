"""B3 common-runner adapter under development; deliberately not registered."""

import threading
import time
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa

from chaos.arms.base import Admission, Arm
from chaos.baselines import faults
from chaos.baselines.boundaries import Boundaries
from chaos.baselines.observer import Observer
from chaos.baselines.registration import messages
from chaos.baselines.runtime import provider_key
from chaos.baselines.stack import Stack
from chaos.baselines.store import Conflict, Request
from chaos.baselines.trace import read_trace
from chaos.baselines.workflow import WorkflowRequest
from chaos.broker_loss import CrashedBrokerLoss, QueuedLoss
from chaos.broker_redelivery import DispatchRedelivery, restore_unacked
from chaos.database_trace import _interval, lease_rows
from chaos.recovery_faults import release_after_escalation
from chaos.scenarios import CONNECTORS


class B3Arm(Boundaries,Arm):
    name='b3'
    execution_mode='b3'
    not_applicable=frozenset({'F15'})
    http_trace_enabled=database_trace_enabled=execution_trace_enabled=broker_trace_enabled=True

    def setup(self,slot,workdir):
        self.slot=slot;self.lock=threading.RLock();self.database=[];self.errors=[]
        self._operations=set();self._workflows=set();self._broker_loss=None;self.scenario=None
        self.boundary_events=[]
        workdir=Path(workdir);workdir.parent.mkdir(parents=True,exist_ok=True)
        components=('worker','relay','reconciler','advancer','api') if self.execution_mode=='b3' else ('worker','relay','reconciler','advancer')
        self.stack=Stack(slot,workdir,execution_mode=self.execution_mode,components=components);self.env=self.stack.env;self.stack.start()
        self.parked_marker=self.stack.trace_dir/'parked'
        # Complete a real delivery before scenario timing, rather than equating PID with readiness.
        from chaos.scenarios import OperationSpec, Scenario
        probe=Scenario('WARMUP','warmup','CONTROL','payments','q-v2',0,(),(),True,10)
        receipt=self.submit(probe,0,OperationSpec('warmup-'+self.stack.queue,100))
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            self.stack.check_alive()
            if self.observe([receipt.local_id])[receipt.local_id].state=='SUCCEEDED':return self
            time.sleep(.05)
        raise RuntimeError('B3 did not complete its warm-up refund')

    def prepare(self,scenario):
        self.stack.check_alive();self.scenario=scenario
        with self.lock:self._operations.clear();self._workflows.clear();self.database.clear()
        self._broker_since_ns=time.monotonic_ns();self.boundary_events.clear()
        if scenario.family=='F14':self.prepare_review(scenario)

    def submit(self,scenario,index,operation,*,changed=False):
        account='acct-'+scenario.scenario_id.lower();key=f'{scenario.scenario_id}-{index}'
        amount=operation.amount_minor+int(changed)
        try:
            if scenario.workflow is not None:
                spec=scenario.workflow
                receipt=self.stack.create_workflow(WorkflowRequest(tenant_id='tenant-north',requester_id='qualification',
                    request_key=key,customer_id=spec.customer_id,case_id=spec.case_id,sku=spec.sku+('-changed' if changed else ''),
                    fee_minor=operation.amount_minor,payments_account_id=account,crm_account_id=account,
                    charge_contract=spec.charge_contract,order_contract=spec.order_contract,
                    compensation_contract=spec.compensation_contract))
                local_id=str(receipt.workflow_id)
                with self.lock:self._workflows.add(local_id)
            else:
                connector,action=CONNECTORS[scenario.connector]
                payload={'payments':dict(charge_id=operation.resource_id,amount_minor=amount),
                    'crm':dict(case_id=operation.resource_id,sku=f'sku_{amount}'),
                    'email':dict(customer_id=operation.resource_id,template=f't_{amount}')}[scenario.connector]
                receipt=self.stack.submit(Request.from_payload(tenant_id='tenant-north',requester_id='qualification',
                    request_key=key,connector_id=connector,contract_version=scenario.contract_version,
                    provider_account_id=account,action_type=action,resource_id=operation.resource_id,payload=payload))
                local_id=str(receipt.operation_id)
                with self.lock:self._operations.add(local_id)
        except Conflict:return Admission(None,'conflict')
        return Admission(local_id)

    def _sample(self,local_ids=None):
        with self.lock:
            operations=set(self._operations);workflows=set(self._workflows)
        if local_ids is not None:
            requested=set(local_ids)
            if requested-(operations|workflows):raise RuntimeError('Requested B3 admission is not tracked')
            operations&=requested;workflows&=requested
        try:
            sample=Observer(self.env.engine,self.env.t,mode=self.execution_mode).sample(operation_ids=operations,workflow_ids=workflows)
            with self.lock:self.database.extend(sample.database)
            return sample
        except Exception as error:self.errors.append('database_observer:'+type(error).__name__);raise

    def observe(self,local_ids):
        if not self.stack._quiescing:
            if self.stack.errors or self.stack.controller.errors or not self.stack.controller.thread.is_alive():self.stack.check_alive()
            for name in self.stack.components:
                process=self.stack.processes[name]
                if process.poll() is not None and process.pid not in self.stack._expected_exits:
                    self.errors.append('unexpected_process_exit:'+name);raise RuntimeError('B3 owned process exited unexpectedly')
        return self._sample(local_ids).operations

    def execution_ids(self,local_ids):
        sample=self._sample(local_ids)
        return sorted({e['operation_id'] for e in sample.database if e['event']=='db_operation_sample'})

    def database_observations(self,local_ids):
        self._sample(local_ids)
        with self.lock:return list(self.database)

    def trace_observations(self,since_ns):return read_trace(self.stack.trace_dir,self.env.t.schema,'http',since_ns)
    def execution_observations(self,since_ns):return read_trace(self.stack.trace_dir,self.env.t.schema,'execution',since_ns)
    def broker_observations(self,since_ns):return read_trace(self.stack.trace_dir,self.env.t.schema,'broker',since_ns)
    def fired_failpoints(self):return faults.fired(self.stack.trace_dir)
    def arm_failpoint(self,point,action):faults.arm(self.stack.trace_dir,point,action)
    def kill_parked_worker(self,point,emit):faults.kill_parked(self.stack,point,emit)

    def _broker_admitted(self):
        with self.lock:return set(self._operations)

    def _broker_unclaimed(self,messages):
        t=self.env.t;started=time.monotonic_ns()
        with self.env.engine.begin() as c:
            c.exec_driver_sql('SET TRANSACTION READ ONLY')
            rows=c.execute(sa.select(t.deliveries,t.jobs.c.status,t.jobs.c.version).join(t.jobs).where(
                t.deliveries.c.delivery_id.in_([UUID(m['delivery_id']) for m in messages]))).mappings().all()
        finished=time.monotonic_ns();keys=('operation_id','outbox_id','delivery_id')
        if {tuple(str(r[k]) for k in keys) for r in rows if r['status']=='RECORDED'}!={tuple(m[k] for k in keys) for m in messages}:
            raise RuntimeError('B3 queued deliveries are not independently committed and unclaimed')
        self.database.extend(dict(_interval(started,finished),event='db_broker_pending',
            **{k:str(r[k]) for k in keys},state=r['status'],transition_seq=r['version']) for r in rows)

    def _broker_dispatching(self,message):
        t=self.env.t;started=time.monotonic_ns()
        with self.env.engine.begin() as c:
            c.exec_driver_sql('SET TRANSACTION READ ONLY')
            rows=c.execute(sa.select(t.jobs,t.attempts.c.attempt_id,
                (t.jobs.c.lease_until>sa.func.clock_timestamp()).label('active')).join(t.deliveries,
                t.deliveries.c.operation_id==t.jobs.c.operation_id).join(t.attempts,
                sa.and_(t.attempts.c.operation_id==t.jobs.c.operation_id,t.attempts.c.claim_token==t.jobs.c.claim_token)).where(
                t.jobs.c.operation_id==UUID(message['operation_id']),t.deliveries.c.outbox_id==UUID(message['outbox_id']),
                t.deliveries.c.delivery_id==UUID(message['delivery_id']))).mappings().all()
        finished=time.monotonic_ns()
        if len(rows)!=1 or rows[0]['status']!='DISPATCHING' or rows[0]['active'] is not True:
            raise RuntimeError('B3 original dispatch fence and live lease were not independently observed')
        row=rows[0];key=provider_key(row['operation_id']) if row['connector_id']=='mock-payments' else str(row['operation_id'])
        sample=lease_rows([(row['operation_id'],row['version'],row['claim_token'],row['attempt_id'],
            row['lease_until'].timestamp(),row['active'],row['connector_id'],key)],started,finished)[0]
        self.database.append(dict(sample,**message,event='db_broker_dispatching',state='DISPATCHING'))

    def begin_broker_loss(self,emit,*,kill=False):
        if getattr(self.slot,'_lock_fd',None) is None:raise RuntimeError('B3 broker loss requires the owned slot lock')
        kind=CrashedBrokerLoss if kill else QueuedLoss
        options=dict(broker=self.slot.private_broker) if kill else {}
        self._broker_loss=kind(self.stack,self.slot.redis(),self.slot.redis_db,self.stack.queue,
            self._broker_admitted,self._broker_unclaimed,lambda:self.broker_observations(self._broker_since_ns),
            emit,protocol='baseline',**options)
        self._broker_loss.begin()

    def finish_broker_loss(self,cancelled):self._broker_loss.finish(cancelled)

    def redeliver_during_dispatch(self,held,cancelled,emit):
        if getattr(self.slot,'_lock_fd',None) is None:raise RuntimeError('B3 redelivery requires the owned slot lock')
        DispatchRedelivery(self.slot.redis(),self.slot.redis_db,self.stack.queue,self._broker_admitted,
            self._broker_dispatching,lambda:self.broker_observations(self._broker_since_ns),
            lambda tag:restore_unacked(self.slot.redis_url,tag),emit,protocol='baseline').run(held,cancelled)

    def release_late_commit(self,held,cancelled,emit):
        def sample():return [r for r in self._sample().database if r['event']=='db_recovery_sample']
        return release_after_escalation(held,cancelled,sample,emit)

    def sample_workflows_before_refund(self):
        # Read committed roots even when an HTTP child outruns its admission response.
        try:
            t=self.env.t
            with self.env.engine.connect() as c:
                ids=c.scalars(sa.select(t.workflows.c.workflow_id).where(t.workflows.c.tenant_id=='tenant-north',
                    t.workflows.c.request_key==f'{self.scenario.scenario_id}-0')).all()
            if len(ids)!=1:raise RuntimeError('B3 refund has no unique admitted workflow')
            with self.lock:self._workflows.update(map(str,ids))
            self._sample(list(map(str,ids)))
        except Exception as error:self.errors.append('before_refund:'+type(error).__name__);raise

    def check_integrity(self):return sorted(set(self.errors+self.stack.integrity_errors()))

    def infrastructure_observations(self,since_ns):
        return dict(protocol='b3-registration-v1',
            registration=[r for r in messages(self.stack.registration_dir,self.env.t.schema) if r['monotonic_ns']>=since_ns],
            registration_database=[r for r in self.stack.controller.events if r['monotonic_ns']>=since_ns],
            processes=list(self.stack.events))

    def quiesce(self):
        if self._broker_loss is not None:self._broker_loss.resume()
        return self.stack.quiesce()

    def teardown(self):
        try:self.stack.close()
        finally:
            if hasattr(self,'_tokens'):self._tokens.clear()
