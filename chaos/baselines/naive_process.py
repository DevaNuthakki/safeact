"""B0 synchronous bounded retries in a killable process, with no database."""

import argparse
import json
import math
import os
import select
import signal
import sys
import threading
import time
from contextlib import ExitStack
from dataclasses import asdict
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID,uuid4

import httpx2

from chaos.baselines.keys import payment_key
from chaos.baselines.processes import Hook,private_path
from chaos.baselines.recovery import retry_delay
from chaos.baselines.store import Request,SCHEMA
from chaos.baselines.trace import Trace,unique_object
from chaos.baselines.workflow import WorkflowRequest,child
from chaos.profiles import profile
from connectors.base import DispatchKind
from connectors.crm import CrmConnector
from connectors.email import EmailConnector
from connectors.http_providers import HttpCrmProvider,HttpEmailProvider,HttpPaymentsProvider
from connectors.http_trace import correlation_hash
from connectors.payments import PaymentsConnector
from core.workflow import workflow_status


FRAME_LIMIT=262144


def read_frame(stream):
    line=stream.readline(FRAME_LIMIT+1)
    if not line or len(line.encode())>FRAME_LIMIT or not line.endswith('\n'):raise ValueError('Incomplete B0 control frame')
    value=json.loads(line,object_pairs_hook=unique_object)
    if not isinstance(value,dict):raise ValueError('Invalid B0 control frame')
    return value


class Caller:
    def __init__(self,config):
        value=json.loads(private_path(config).read_text(),object_pairs_hook=unique_object)
        if set(value)!={'scope','providers','trace_dir'} or not SCHEMA.fullmatch(value['scope']):raise ValueError('Invalid B0 private configuration')
        if set(value['providers'])!={'payments','crm','email'}:raise ValueError('Three local mock providers required')
        for address in value['providers'].values():
            url=urlsplit(address)
            if url.scheme!='http' or url.hostname!='127.0.0.1' or not url.port or url.username or url.password or url.path not in ('','/') or url.query or url.fragment:
                raise ValueError('B0 is restricted to local mock providers')
        self.config=value;self.stop=threading.Event();self.hook=Hook(value['trace_dir'],value['scope'])
        signal.signal(signal.SIGTERM,lambda *_:self.stop.set());signal.signal(signal.SIGINT,lambda *_:self.stop.set())
        self.steps={};self.workflow=None;self.root=None;self.stack=ExitStack()
        observer=Trace(value['trace_dir'],value['scope'],'http')
        clients={k:self.stack.enter_context(httpx2.Client(base_url=v,trust_env=False)) for k,v in value['providers'].items()}
        self.connectors={'mock-payments':PaymentsConnector(HttpPaymentsProvider(value['providers']['payments'],client=clients['payments'],observer=observer)),
            'mock-crm':CrmConnector(HttpCrmProvider(value['providers']['crm'],client=clients['crm'],observer=observer)),
            'mock-email':EmailConnector(HttpEmailProvider(value['providers']['email'],client=clients['email'],observer=observer))}

    def close(self):self.stack.close()

    def exchange(self,kind,**fields):
        if self.stop.is_set():raise SystemExit(0)
        nonce=str(uuid4())
        frame=dict(kind=kind,nonce=nonce,pid=os.getpid(),monotonic_ns=time.monotonic_ns(),**fields)
        line=json.dumps(frame,allow_nan=False,separators=(',',':'))+'\n'
        if len(line.encode())>FRAME_LIMIT:raise ValueError('Oversized B0 control frame')
        sys.stdout.write(line);sys.stdout.flush();deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if self.stop.is_set():raise SystemExit(0)
            if select.select([sys.stdin],[],[],.05)[0]:
                answer=read_frame(sys.stdin)
                if answer!=dict(kind='ack',nonce=nonce,pid=os.getpid()):raise ValueError('Foreign B0 control acknowledgement')
                return
        raise RuntimeError('B0 control acknowledgement expired')

    def snapshot(self):
        if self.workflow is None:self.exchange('operation',operation_id=str(self.root),snapshot=self.steps['operation'])
        else:
            by=self.steps
            try:status=workflow_status(*(by[k]['state'] if k in by else None for k in ('charge','order','compensation'))).value
            except ValueError:status='IN_PROGRESS'
            self.exchange('workflow',operation_id=str(self.root),snapshot=dict(workflow_id=str(self.root),
                tenant_id=self.workflow.tenant_id,requester_id=self.workflow.requester_id,request_key=self.workflow.request_key,
                request=asdict(self.workflow),steps=list(by.values()),status=status))

    def new_step(self,role,request,*,compensates=None):
        op=uuid4();kind=profile(request.connector_id,request.contract_version)
        key=payment_key(op,1,'b0') if kind.contract.native_idempotency else str(op)
        row=dict(request.__dict__);payload=json.loads(row.pop('payload_canonical'))
        step=dict(row,operation_id=str(op),payload=payload,state='RECORDED',step=role,
            compensates_operation_id=compensates,correlation_sha256=correlation_hash(key),
            correlation_sha256s=[correlation_hash(key)],native_key_mode='b0',attempt_ordinals=[],
            applied_reference=None,resolved_at=None,escalated_at=None,evidence_rows=0)
        self.steps[role]=step;return step

    def dispatch(self,role,request):
        step=self.steps[role];op=UUID(step['operation_id']);kind=profile(request.connector_id,request.contract_version)
        first=None;all_rejected=True
        for ordinal in range(1,kind.max_attempts+1):
            if self.stop.is_set():return
            if first is not None and time.monotonic()-first>=kind.investigation_window.total_seconds():break
            native=payment_key(op,ordinal,'b0') if kind.contract.native_idempotency else None
            if native:self.exchange('profile',operation_id=str(op),ordinal=ordinal,key=native,connector_id=request.connector_id,version=request.contract_version)
            if first is None:first=time.monotonic()
            step['attempt_ordinals'].append(ordinal);wire=native or str(op)
            step['correlation_sha256']=correlation_hash(wire)
            step['correlation_sha256s']=sorted({correlation_hash(payment_key(op,n,'b0') if native else str(op)) for n in step['attempt_ordinals']})
            step['state']='DISPATCHING';self.snapshot()
            details=dict(operation_id=str(op),attempt_id=str(uuid4()));self.hook('before_provider_call',details)
            intent=SimpleNamespace(**request.immutable(),operation_id=op,provider_idempotency_key=native)
            result=self.connectors[request.connector_id].dispatch(intent,operation_id=op,timeout=kind.request_deadline.total_seconds())
            self.hook('after_provider_call',details)
            all_rejected=all_rejected and result.kind is DispatchKind.NOT_APPLIED
            if result.kind is DispatchKind.APPLIED:
                step.update(state='SUCCEEDED',applied_reference=result.reference,resolved_at=time.time());self.snapshot();return
            if all_rejected and not result.retryable:
                step.update(state='FAILED_CONFIRMED',resolved_at=time.time());self.snapshot();return
            step['state']='UNKNOWN';self.snapshot()
            if ordinal<kind.max_attempts:
                pause=retry_delay(kind,request.request_key,ordinal,retry_after=float(dict(result.provider_facts).get('retry_after',0)))
                self.stop.wait(pause.total_seconds())
        step['state']='UNKNOWN';self.snapshot()

    def run(self,message):
        if set(message)!={'kind','workflow','request'} or message['kind']!='submit' or type(message['workflow']) is not bool:raise ValueError('Invalid B0 submission')
        self.root=uuid4()
        if message['workflow']:
            self.workflow=WorkflowRequest(**message['request'])
            self.exchange('accepted',operation_id=str(self.root),workflow=True,request=asdict(self.workflow))
            charge=child(self.workflow,self.root,'charge');self.new_step('charge',charge);self.snapshot();self.dispatch('charge',charge)
            if self.steps['charge']['state']=='SUCCEEDED':
                order=child(self.workflow,self.root,'order');self.new_step('order',order);self.snapshot();self.dispatch('order',order)
                if self.steps['order']['state']=='FAILED_CONFIRMED':
                    inverse=child(self.workflow,self.root,'compensation',charge_reference=self.steps['charge']['applied_reference'])
                    self.new_step('compensation',inverse,compensates=self.steps['charge']['operation_id']);self.snapshot();self.dispatch('compensation',inverse)
        else:
            request=Request(**message['request']);self.root=uuid4()
            step=self.new_step('operation',request);self.root=UUID(step['operation_id'])
            self.exchange('accepted',operation_id=str(self.root),workflow=False,request=request.__dict__)
            self.snapshot();self.dispatch('operation',request)
        self.exchange('finished',operation_id=str(self.root))


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',required=True);args=parser.parse_args()
    caller=Caller(args.config)
    try:caller.run(read_frame(sys.stdin))
    finally:caller.close()


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('BASELINE_NAIVE_ERROR:'+type(error).__name__,file=sys.stderr);raise SystemExit(1)
