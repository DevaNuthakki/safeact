"""Nine local B3 execution development checks; not a comparison/qualification arm.

PYTHONPATH=. SAFEACT_RUN_BASELINE_DEV_CHECK=1 .venv/bin/python smoke.py --out /tmp/new-output
"""
import argparse,hashlib,json,os,platform,signal,subprocess,sys,tempfile,time
from pathlib import Path
from uuid import uuid4
import sqlalchemy as sa
from chaos.baselines.environment import Environment
from chaos.baselines.processes import Config
from chaos.baselines.runtime import provider_key
from chaos.baselines.store import Request,admit
from chaos.provider_trace import ObservedProvider
from chaos.recovery_faults import ShortRetentionPayments
from connectors.crm_mock import MockCrmProvider
from connectors.email_mock import MockEmailProvider
from connectors.http_mock.servers import MockProviderServers
from connectors.ledger import EffectsLedger
from connectors.http_trace import ObservedClient

CASES=('control-payments','control-crm','control-email','reply-lost-crm','ambiguous-email',
       'native-replay','worker-crash-after-send','late-crm-after-empty','lookup-past-key-window')
def require(value,message):
    if not value:raise RuntimeError(message)
def dump(path,value):path.write_text(json.dumps(value,indent=2,default=str)+'\n')
def snapshot(env,label):
    started=time.monotonic_ns()
    with env.engine.begin() as c:
        c.exec_driver_sql('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        data={name:[dict(row) for row in c.execute(sa.select(getattr(env.t,name))).mappings().all()]
              for name in ('jobs','attempts','observations','audit','outbox','deliveries','accounts')}
        user=c.scalar(sa.text('SELECT current_user'))
    return dict(event='independent_database_snapshot',label=label,source='database',application_role=user,
                sample_started_ns=started,sample_finished_ns=time.monotonic_ns(),tables=data)
def run_case(name,out):
    raw=dict(case=name,scope='development smoke; not frozen qualification or a registered arm',
             snapshots=[],timeline=[],harness_errors=[],http=[],processes=[])
    env=Environment();servers=None;process=None;held=None;ctx=None;log=None
    ledger=EffectsLedger(); started=time.monotonic()
    def event(label,**details):raw['timeline'].append(dict(event=label,monotonic_ns=time.monotonic_ns(),**details))
    with tempfile.TemporaryDirectory(prefix='safeact-b3-smoke-') as private:
        try:
            env.start()
            payments=ShortRetentionPayments(ledger=ledger);crm=MockCrmProvider(ledger=ledger);email=MockEmailProvider(ledger=ledger)
            observed=tuple(ObservedProvider(k,m,ledger.record_observation) for k,m in (('payments',payments),('crm',crm),('email',email)))
            servers=MockProviderServers(ledger=ledger,providers=observed,stall_seconds=1.5).start()
            config=Path(private)/'private.json';env.write_config(config,servers.urls)
            ctx=Config(config).runtime();runtime=ctx.__enter__()
            def http_event(event, **details):
                raw['http'].append(dict(event=event,source='http',pid=os.getpid(),
                                        monotonic_ns=time.monotonic_ns(),**details))
            for connector in runtime.connectors.values():
                provider=connector._provider
                provider._http=ObservedClient(connector.connector_id,provider._http,http_event)
            kind=('payments' if name in ('control-payments','native-replay','lookup-past-key-window') else
                  'email' if name in ('control-email','ambiguous-email') else 'crm')
            version='q-v2-short' if name=='lookup-past-key-window' else 'q-v2'
            resource='target_'+uuid4().hex
            payload={'payments':dict(charge_id=resource,amount_minor=500),'crm':dict(case_id=resource,sku='replacement'),
                     'email':dict(customer_id=resource,template='confirmation')}[kind]
            action={'payments':'refund_payment','crm':'create_replacement_order','email':'send_refund_confirmation'}[kind]
            request=Request.from_payload(tenant_id='north',requester_id='requester',request_key='smoke-'+name,
                connector_id='mock-'+kind,contract_version=version,provider_account_id='acct-'+uuid4().hex,
                action_type=action,resource_id=resource,payload=payload)
            operation=admit(env.engine,env.t,request).operation_id
            key=provider_key(operation) if kind=='payments' else str(operation)
            if kind=='payments':payments.register_key(key,version)
            raw.update(operation_id=str(operation),request=request.immutable(),correlation_sha256=hashlib.sha256(key.encode()).hexdigest())
            def view(label):
                value=snapshot(env,label);raw['snapshots'].append(value);return value['tables']['jobs'][0]
            def task():
                tasks=[];runtime.relay_once(tasks.append);require(len(tasks)==1,'Expected one committed delivery');return tasks[0]
            def until(state,seconds=10):
                deadline=time.monotonic()+seconds
                while time.monotonic()<deadline:
                    runtime.reconcile(operation)
                    row=view('settling')
                    if row['status']==state:return row
                    time.sleep(.05)
                raise RuntimeError('Expected terminal state '+state)
            def hook(point,details):
                event(point,**details)
                if point=='before_provider_call':
                    row=view('provider_fence_visible')
                    require(row['status']=='DISPATCHING','Provider dispatch preceded its committed fence')
            runtime.hook=hook
            if name in ('reply-lost-crm',):crm.fail_after_effect=True
            if name=='ambiguous-email':email.fail_after_effect=True
            if name in ('native-replay','lookup-past-key-window'):payments.fail_after_effect=True
            if name=='native-replay':payments.lookup_unavailable=True
            if name=='late-crm-after-empty':held=crm.hold_next(respond_now=True)
            admitted=task();view('admitted')
            if name=='worker-crash-after-send':
                faults=Path(private)/'faults';faults.mkdir(mode=0o700)
                armed=faults/'after_provider_call';armed.write_text('park');armed.chmod(0o600)
                worker_config=Path(private)/'worker.json';env.write_config(worker_config,servers.urls,fault_dir=faults)
                log=open(Path(private)/'worker.log','wb')
                command=[sys.executable,'-m','chaos.baselines.processes','--config',str(worker_config),'execute','--task',
                         str(operation),str(admitted.outbox_id),str(admitted.delivery_id)]
                process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                deadline=time.monotonic()+12
                while not (faults/'parked').exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.02)
                require((faults/'parked').exists(),'Worker did not reach the actual provider-call checkpoint')
                marker=json.loads((faults/'parked').read_text())
                require(marker['pid']==process.pid and os.getpgid(process.pid)==process.pid,'Wrong owned worker process')
                row=view('effect_before_worker_death')
                require(row['status']=='DISPATCHING' and len(ledger.effects_for(key))==1,'Missing effect/fence before kill')
                event('worker_kill_requested',pid=process.pid,signal=9)
                os.killpg(process.pid,signal.SIGKILL);code=process.wait(timeout=5)
                event('worker_exit_observed',pid=process.pid,returncode=code)
                require(code==-9,'Owned worker did not exit from SIGKILL')
                raw['processes'].append(dict(pid=process.pid,returncode=code,marker=marker,
                    trace=[json.loads(line) for p in faults.glob('trace-*.jsonl') for line in p.read_text().splitlines()]))
                until('SUCCEEDED')
            else:
                runtime.execute(admitted);row=view('dispatch_observed')
                if name=='native-replay':
                    until('RETRY_ELIGIBLE');runtime.execute(task());view('native_replayed')
                elif name=='late-crm-after-empty':
                    runtime.reconcile(operation);row=view('empty_lookup_before_commit')
                    require(row['status']=='UNKNOWN' and len(ledger.effects_for(key))==0,'Empty lookup did not preserve ambiguity')
                    require(len(ledger.calls_for(key))==1,'An unprotected repeat was sent')
                    held.release();require(held.wait_committed(2),'Late provider commit did not finish')
                    event('held_commit_observed');until('SUCCEEDED')
                elif name=='lookup-past-key-window':
                    first=row['first_fence_at'];old=payments.lookup
                    from datetime import timedelta
                    def delayed(k):
                        while True:
                            with env.engine.connect() as c:now=runtime.now(c)
                            if now>=first+timedelta(seconds=6.1):break
                            time.sleep(.02)
                        return old(k)
                    payments.lookup=delayed
                    while True:
                        with env.engine.connect() as c:now=runtime.now(c)
                        if now>=first+timedelta(seconds=3):break
                        time.sleep(.02)
                    runtime.reconcile(operation);late=view('lookup_completed_after_expiry')
                    require(not any(o['kind']=='not_applied' for o in raw['snapshots'][-1]['tables']['observations']),
                            'Late empty answer became authoritative proof')
                    until('ESCALATED_UNRESOLVED')
                elif name=='reply-lost-crm':until('SUCCEEDED')
            expected='ESCALATED_UNRESOLVED' if name in ('ambiguous-email','lookup-past-key-window') else 'SUCCEEDED'
            final=view('observed_terminal')
            require(final['status']==expected,'Unexpected observed terminal state')
            require(len(ledger.effects_for(key))==1,'Expected exactly one independent effect')
            raw.update(expected_outcome=expected,completion_achievable=expected=='SUCCEEDED',outcome=final['status'])
            event('observation_window_closed')
        except Exception as error:
            raw['harness_errors'].append(dict(type=type(error).__name__,message=str(error)))
        finally:
            if held:held.release();held.wait_committed(3)
            if process and process.poll() is None:
                os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5)
            if ctx:ctx.__exit__(None,None,None)
            if log:log.close()
            if servers:servers.stop()
            raw['ledger']=ledger.events();raw['elapsed_seconds']=time.monotonic()-started
            if env.created:raw['snapshots'].append(snapshot(env,'after_execution_and_provider_shutdown'))
            env.stop()
            raw['cleanup_complete']=not env.created and (process is None or process.poll() is not None)
            dump(out/(name+'.json'),raw)
    return raw

def main():
    p=argparse.ArgumentParser(description=__doc__.splitlines()[0]);p.add_argument('--out',required=True);a=p.parse_args()
    require(os.environ.get('SAFEACT_RUN_BASELINE_DEV_CHECK')=='1','Explicit local baseline development opt-in required')
    out=Path(a.out);out.mkdir(parents=True,exist_ok=False)
    rows=[]
    for name in CASES:
        row=run_case(name,out);rows.append(row)
        print(name, 'errors='+str(len(row['harness_errors'])),row.get('outcome','ERROR'),flush=True)
    errors=sum(bool(r['harness_errors']) for r in rows)
    summary=dict(scope='B3 execution development smoke; not frozen qualification, baseline comparison or a load result',
        environment=dict(platform=platform.system(),python=platform.python_version(),load_average=os.getloadavg(),local_http_mocks_only=True),
        cases=len(rows),errors=errors,achievable=sum(r.get('completion_achievable',False) for r in rows),
        completed=sum(r.get('outcome')=='SUCCEEDED' and not r['harness_errors'] for r in rows),
        unresolved=sum(r.get('outcome')=='ESCALATED_UNRESOLVED' for r in rows),
        effects=sum(sum(e['event']=='effect' for e in r['ledger']) for r in rows),
        dispatch_calls=sum(sum(e['event']=='provider_start' and e.get('method') in ('refund','charge','create_record','send') for e in r['ledger']) for r in rows),
        all_cleanup_complete=all(r['cleanup_complete'] for r in rows),external_provider_calls=0,paid_model_calls=0)
    dump(out/'summary.json',summary);print(json.dumps(summary),flush=True)
    return int(errors>0 or not summary['all_cleanup_complete'])
if __name__=='__main__':sys.exit(main())
