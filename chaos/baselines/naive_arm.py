"""B0 caller and owned subprocesses: observed acceptance is memory-only."""

import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import copy
from dataclasses import asdict
from pathlib import Path
from uuid import UUID,uuid4

from chaos.arms.base import Admission,Arm,OperationView
from chaos.baselines import faults
from chaos.baselines.keys import payment_key
from chaos.baselines.naive_process import FRAME_LIMIT,read_frame
from chaos.baselines.processes import private_path
from chaos.baselines.store import Request
from chaos.baselines.trace import read_trace,uuid_text
from chaos.baselines.workflow import WorkflowRequest,child
from chaos.profiles import profile
from chaos.quiescence import stop_processes
from chaos.scenarios import CONNECTORS
from chaos.workflow_oracle import wire_keys


ROOT=Path(__file__).resolve().parents[2]


class NaiveStack:
    def __init__(self,owner,workdir):
        self.owner=owner;self.workdir=Path(workdir);self.trace_dir=self.workdir/'traces'
        # This is an owned trace-scope identifier; B0 never creates a DB schema.
        self.scope='safeact_baseline_it_'+uuid4().hex
        self.config_path=self.workdir/'private-config.json';self.processes={};self.generations=[];self.logs=[]
        self.lock=threading.RLock();self.errors=[];self.events=[];self.expected={};self.quiescing=False;self.stopped=False

    def start(self):
        self.workdir.mkdir(mode=0o700,parents=True,exist_ok=False);self.trace_dir.mkdir(mode=0o700)
        value=dict(scope=self.scope,providers={k:f'http://127.0.0.1:{p}' for k,p in self.owner.slot.proxy_ports.items()},trace_dir=str(self.trace_dir))
        fd=os.open(self.config_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream:json.dump(value,stream)
        return self

    def submit(self,message):
        with self.lock:
            if self.quiescing:raise RuntimeError('B0 caller is stopping')
            path=self.workdir/f'worker-{len(self.generations)}.log'
            descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            log=os.fdopen(descriptor,'wb');self.logs.append(log)
            process=subprocess.Popen([sys.executable,'-m','chaos.baselines.naive_process','--config',str(self.config_path)],
                cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,text=True,bufsize=1,start_new_session=True)
            old=self.processes.get('worker')
            if old is not None:self.processes[f'worker:{old.pid}']=old
            self.processes['worker']=process
            meta=dict(process=process,path=path,message=message,root=None,accepted=threading.Event(),finished=False,
                nonces=set(),last_ns=0,requests={},steps={},thread=None)
            self.generations.append(meta);self.events.append(dict(event='b0_process_started',pid=process.pid,monotonic_ns=time.monotonic_ns()))
            thread=threading.Thread(target=self._read,args=(meta,),name=f'b0-observer-{process.pid}',daemon=True);meta['thread']=thread;thread.start()
        process.stdin.write(json.dumps(message,allow_nan=False)+'\n');process.stdin.flush()
        if not meta['accepted'].wait(12):raise RuntimeError('B0 process did not announce its actual acceptance')
        if meta['root'] is None:raise RuntimeError('B0 acceptance failed')
        return meta['root']

    def _read(self,meta):
        p=meta['process']
        try:
            while True:
                if not (line:=p.stdout.readline(FRAME_LIMIT+1)):break
                if len(line.encode())>FRAME_LIMIT or not line.endswith('\n'):raise ValueError('Incomplete B0 observation')
                from chaos.baselines.trace import unique_object
                frame=json.loads(line,object_pairs_hook=unique_object);received=time.monotonic_ns()
                if (not isinstance(frame,dict) or frame.get('pid')!=p.pid or not uuid_text(frame.get('nonce'))
                        or frame['nonce'] in meta['nonces'] or type(frame.get('monotonic_ns')) is not int
                        or not meta['last_ns']<frame['monotonic_ns']<=received):raise ValueError('Foreign or stale B0 observation')
                meta['nonces'].add(frame['nonce']);meta['last_ns']=frame['monotonic_ns']
                base={'kind','nonce','pid','monotonic_ns','operation_id'};kind=frame['kind']
                extras={'accepted':{'workflow','request'},'profile':{'ordinal','key','connector_id','version'},
                    'operation':{'snapshot'},'workflow':{'snapshot'},'finished':set()}
                if kind not in extras or set(frame)!=base|extras[kind] or not uuid_text(frame.get('operation_id')):raise ValueError('Unexpected B0 observation fields')
                with self.owner.lock:self._frame(meta,frame,received)
                p.stdin.write(json.dumps(dict(kind='ack',nonce=frame['nonce'],pid=p.pid))+'\n');p.stdin.flush()
                if kind=='accepted':meta['accepted'].set()
        except Exception as error:
            expected_pipe_close=isinstance(error,(BrokenPipeError,ConnectionError)) and (self.quiescing or p.pid in self.expected)
            if not expected_pipe_close:self.errors.append('b0_control:'+type(error).__name__)
            try:
                if p.poll() is None:os.killpg(p.pid,signal.SIGTERM)
            except ProcessLookupError:pass
        finally:
            meta['accepted'].set()
            with self.owner.lock:
                root=meta['root']
                if root is not None and not meta['finished'] and root in self.owner.views:
                    previous=self.owner.views[root]
                    if previous.outcome_class=='pending':previous.state='WORKER_EXITED_UNKNOWN'
            p.stdout.close();p.stdin.close()

    def _frame(self,meta,frame,received):
        kind=frame['kind'];op=frame['operation_id']
        if kind=='accepted':
            if meta['root'] is not None or frame['workflow']!=meta['message']['workflow'] or frame['request']!=meta['message']['request']:
                raise ValueError('B0 acceptance differs from the caller request')
            meta['root']=op
            self.owner.views[op]=OperationView(op,'pending','ACCEPTED_IN_MEMORY')
            if not frame['workflow']:meta['requests'][op]=Request(**frame['request'])
            self.owner.acceptance.append(dict(source='process',event='b0_acceptance_observed',operation_id=op,pid=frame['pid'],
                sample_started_ns=frame['monotonic_ns'],sample_finished_ns=received,monotonic_ns=received,durable=False))
            return
        if meta['root'] is None:raise ValueError('B0 acted before observed acceptance')
        if kind=='profile':
            request=meta['requests'].get(op)
            if request is None or request.connector_id!=frame['connector_id'] or request.contract_version!=frame['version']:
                raise ValueError('B0 registration has no exact observed intent')
            contract=profile(request.connector_id,request.contract_version);ordinal=frame['ordinal']
            if type(ordinal) is not int or not 1<=ordinal<=contract.max_attempts or frame['key']!=payment_key(op,ordinal,'b0') or not contract.contract.native_idempotency:
                raise ValueError('B0 registration uses a foreign key or budget')
            self.owner.slot.mocks['payments'].register_key(frame['key'],frame['version'])
            self.owner.registration.append(dict(source='harness',event='b0_key_registered',operation_id=op,pid=frame['pid'],
                ordinal=ordinal,contract_version=request.contract_version,monotonic_ns=time.monotonic_ns()))
            return
        if op!=meta['root']:raise ValueError('Foreign B0 root observation')
        if kind=='finished':meta['finished']=True;return
        if kind=='operation':
            step=frame['snapshot'];request=meta['requests'][op];self._step(meta,step,request,'operation')
            self.owner.snapshots.append(dict(step, source='process', event='process_operation_sample',
                binding_format='b0-operation-v1', sender_pid=frame['pid'],
                sample_started_ns=frame['monotonic_ns'], sample_finished_ns=received, monotonic_ns=received))
            state=step['state'];outcome={'SUCCEEDED':'confirmed_success','FAILED_CONFIRMED':'confirmed_failure'}.get(state,'pending')
            self.owner.views[op]=OperationView(op,outcome,state,resolved_at=step['resolved_at'],evidence_rows=0)
            return
        snapshot=frame['snapshot'];request=WorkflowRequest(**meta['message']['request'])
        if (snapshot.get('workflow_id')!=op or snapshot.get('request')!=asdict(request)
                or any(snapshot.get(k)!=getattr(request,k) for k in ('tenant_id','requester_id','request_key'))):raise ValueError('Foreign B0 workflow snapshot')
        steps=snapshot.get('steps',[]);roles={s['step']:s for s in steps}
        if len(roles)!=len(steps) or set(roles)-{'charge','order','compensation'} or 'charge' not in roles:raise ValueError('Invalid B0 step set')
        if not set(meta['steps'])<=set(roles):raise ValueError('B0 workflow step disappeared')
        for role,step in roles.items():
            expected=child(request,UUID(op),role,charge_reference=roles['charge'].get('applied_reference') if role=='compensation' else None)
            self._step(meta,step,expected,role)
            if step['compensates_operation_id']!=(roles['charge']['operation_id'] if role=='compensation' else None):raise ValueError('Wrong B0 inverse link')
            meta['requests'][step['operation_id']]=expected
        from core.workflow import workflow_status
        status=workflow_status(*(roles[k]['state'] if k in roles else None for k in ('charge','order','compensation'))).value
        if status!=snapshot['status']:raise ValueError('B0 workflow status differs from its actual step states')
        value=dict(snapshot,source='process',event='process_workflow_sample',binding_format='b0-request-v1',sender_pid=frame['pid'],
            sample_started_ns=frame['monotonic_ns'],sample_finished_ns=received,monotonic_ns=received)
        self.owner.snapshots.append(value)
        outcome={'COMPLETED':'confirmed_success','FAILED_NO_EFFECT':'confirmed_failure','COMPENSATED_VERIFIED':'verified_compensation'}.get(status,'pending')
        times=[s['resolved_at'] for s in steps if s['resolved_at'] is not None]
        self.owner.views[op]=OperationView(op,outcome,status,resolved_at=max(times) if times and outcome!='pending' else None,evidence_rows=0)

    def _step(self,meta,step,request,role):
        if not uuid_text(step.get('operation_id')) or step.get('step')!=role:raise ValueError('Invalid B0 step identity')
        parsed=Request.from_payload(payload=step['payload'],**{k:step[k] for k in Request.__dataclass_fields__ if k!='payload_canonical'})
        if parsed!=request or step['native_key_mode']!='b0':raise ValueError('B0 step changed its authorized request')
        if step['state'] not in ('RECORDED','DISPATCHING','UNKNOWN','SUCCEEDED','FAILED_CONFIRMED'):raise ValueError('Invalid B0 step state')
        if step['state'] in ('SUCCEEDED','FAILED_CONFIRMED'):
            if type(step.get('resolved_at')) not in (int,float) or not math.isfinite(step['resolved_at']) or step['resolved_at']<=0:raise ValueError('B0 terminal response has no actual timestamp')
        if step['state']=='SUCCEEDED' and (type(step.get('applied_reference')) is not str or not step['applied_reference']):raise ValueError('B0 success has no provider reference')
        if step.get('evidence_rows')!=0 or step.get('escalated_at') is not None:raise ValueError('B0 cannot claim database evidence or human escalation')
        wire_keys(step)
        previous=meta['steps'].get(role)
        if previous and (previous['operation_id']!=step['operation_id'] or not set(previous['attempt_ordinals'])<=set(step['attempt_ordinals'])):
            raise ValueError('B0 step identity or attempt history changed')
        meta['steps'][role]=step

    def kill(self,meta):
        p=meta['process']
        if p.poll() is not None or os.getpgid(p.pid)!=p.pid:raise RuntimeError('B0 worker is not this live owned group')
        self.expected[p.pid]=-9;os.killpg(p.pid,signal.SIGKILL)
        if p.wait(timeout=5)!=-9:raise RuntimeError('B0 SIGKILL was not independently observed')

    def stop(self,timeout=7):
        self.quiescing=True
        for meta in self.generations:
            p=meta['process']
            if p.poll() is None:
                try:os.killpg(p.pid,signal.SIGTERM)
                except ProcessLookupError:pass
        deadline=time.monotonic()+timeout
        for meta in self.generations:
            try:meta['process'].wait(timeout=max(.01,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:self.errors.append('b0_graceful_shutdown_timeout')

    def quiesce(self):
        if not self.stopped:
            self.quiescing=True;proof=stop_processes(self,timeout=6)
            for meta in self.generations:
                meta['thread'].join(3)
                if meta['thread'].is_alive():raise RuntimeError('B0 observer thread did not stop')
            self.stopped=True;self.events.append(dict(event='b0_execution_stopped',processes=proof,monotonic_ns=time.monotonic_ns()))
        if self.integrity():raise RuntimeError('B0 retained process/observation errors')
        return self.events[-1]['processes']

    def integrity(self):
        errors=list(self.errors)
        for meta in self.generations:
            p=meta['process'];code=p.poll()
            if code is not None and code!=self.expected.get(p.pid,0):errors.append('b0_unexpected_exit:'+str(code))
            if any(m in meta['path'].read_text() for m in ('BASELINE_NAIVE_ERROR','BASELINE_TRACE_ERROR','SAFEACT_HTTP_TRACE_ERROR')):
                errors.append('b0_process_or_trace_error')
        return sorted(set(errors))

    def close(self):
        try:self.quiesce()
        finally:
            if self.stopped:
                if self.config_path.exists():private_path(self.config_path).unlink()
                for log in self.logs:log.close()


class B0Arm(Arm):
    name='b0'
    not_applicable=frozenset({'F14','F15'})
    capabilities=frozenset({'killable_worker','fixed_workflow'})
    http_trace_enabled=execution_trace_enabled=True
    database_trace_enabled=broker_trace_enabled=False
    point_map={'after_claim_commit':'before_provider_call','after_fence_commit':'before_provider_call','before_outcome_commit':'after_provider_call'}

    def setup(self,slot,workdir):
        self.slot=slot;self.lock=threading.RLock();self.views={};self.snapshots=[];self.acceptance=[];self.registration=[]
        self.scenario=None;self.stack=NaiveStack(self,workdir);self.stack.start();self.parked_marker=self.stack.trace_dir/'parked'
        from chaos.scenarios import OperationSpec,Scenario
        probe=Scenario('WARMUP','warmup','CONTROL','payments','q-v2',0,(),(),True,10)
        receipt=self.submit(probe,0,OperationSpec('b0-warmup-'+uuid4().hex,100))
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            if self.observe([receipt.local_id])[receipt.local_id].state=='SUCCEEDED':return self
            if self.check_integrity():raise RuntimeError('B0 warm-up process failed')
            time.sleep(.05)
        raise RuntimeError('B0 did not complete its warm-up call')

    def prepare(self,scenario):
        self.scenario=scenario
        with self.lock:self.views.clear();self.snapshots.clear();self.acceptance.clear();self.registration.clear()

    def submit(self,scenario,index,operation,*,changed=False):
        account='acct-'+scenario.scenario_id.lower();amount=operation.amount_minor+int(changed)
        if scenario.workflow is not None:
            w=scenario.workflow
            request=WorkflowRequest(tenant_id='tenant-north',requester_id='qualification',request_key=f'{scenario.scenario_id}-{index}',
                customer_id=w.customer_id,case_id=w.case_id,sku=w.sku+('-changed' if changed else ''),fee_minor=operation.amount_minor,
                payments_account_id=account,crm_account_id=account,charge_contract=w.charge_contract,
                order_contract=w.order_contract,compensation_contract=w.compensation_contract)
            message=dict(kind='submit',workflow=True,request=asdict(request))
        else:
            connector,action=CONNECTORS[scenario.connector]
            payload={'payments':dict(charge_id=operation.resource_id,amount_minor=amount),
                'crm':dict(case_id=operation.resource_id,sku=f'sku_{amount}'),
                'email':dict(customer_id=operation.resource_id,template=f't_{amount}')}[scenario.connector]
            request=Request.from_payload(tenant_id='tenant-north',requester_id='qualification',request_key=f'{scenario.scenario_id}-{index}',
                connector_id=connector,contract_version=scenario.contract_version,provider_account_id=account,
                action_type=action,resource_id=operation.resource_id,payload=payload)
            message=dict(kind='submit',workflow=False,request=request.__dict__)
        return Admission(self.stack.submit(message))

    def observe(self,local_ids):
        with self.lock:return {i:copy.deepcopy(self.views[i]) for i in local_ids}

    def arm_failpoint(self,point,action):faults.arm(self.stack.trace_dir,self.point_map.get(point,point),action)
    def fired_failpoints(self):return faults.fired(self.stack.trace_dir)
    def trace_observations(self,since_ns):return read_trace(self.stack.trace_dir,self.stack.scope,'http',since_ns)
    def execution_observations(self,since_ns):return read_trace(self.stack.trace_dir,self.stack.scope,'execution',since_ns)

    def kill_parked_worker(self,point,emit):
        marker=private_path(self.parked_marker);row=json.loads(marker.read_text())
        native=self.point_map.get(point,point)
        if row.get('point')!=native or type(row.get('pid')) is not int:raise RuntimeError('Invalid B0 parked identity')
        candidates=[m for m in self.stack.generations if m['process'].pid==row['pid']]
        if len(candidates)!=1:raise RuntimeError('B0 parked process is not owned')
        meta=candidates[0];p=meta['process']
        details=dict(point=native,declared_point=point,worker_pid=p.pid,parked_pid=p.pid,process_group=p.pid,signal=9)
        emit('worker_kill_requested',**details);self.stack.kill(meta);emit('worker_killed',**details,returncode=p.returncode)
        marker.unlink()

    def sample_workflows_before_refund(self):
        with self.lock:
            if not self.snapshots:raise RuntimeError('B0 refund has no observed child workflow snapshot')
            snapshot=self.snapshots[-1];steps={s['step']:s for s in snapshot['steps']}
            if not {'charge','order','compensation'}<=set(steps) or steps['charge']['state']!='SUCCEEDED' or steps['order']['state']!='FAILED_CONFIRMED':
                raise RuntimeError('B0 refund lacks the actual observed forward precondition')

    def infrastructure_observations(self,since_ns):
        with self.lock:return dict(protocol='b0-process-v1',database_created=False,point_map=self.point_map,
            process_snapshots=[s for s in self.snapshots if s['monotonic_ns']>=since_ns],acceptance=list(self.acceptance),
            registration=list(self.registration),processes=list(self.stack.events))

    def check_integrity(self):return self.stack.integrity()
    def quiesce(self):return self.stack.quiesce()
    def teardown(self):self.stack.close()
