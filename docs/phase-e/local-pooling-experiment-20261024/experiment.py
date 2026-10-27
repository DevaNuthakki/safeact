"""Local pooling/preparation experiment; never invokes frozen qualification.

All four configurations use the same immutable admission and lifecycle code.
A benchmark-only worker adapter changes connection ownership, not transitions.
"""
import contextlib
import hashlib
import json
import math
import os
import platform
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx2 as httpx
import psycopg
import sqlalchemy as sa
from psycopg import sql
from sqlalchemy.pool import NullPool, QueuePool

from chaos.frozen import verify
from connectors.email import EmailConnector
from connectors.email_mock import MockEmailProvider
from connectors.http_mock.servers import MockProviderServers
from connectors.http_providers import HttpPaymentsProvider
from connectors.http_trace import correlation_hash
from connectors.ledger import EffectsLedger
from connectors.payments import PaymentsConnector
from connectors.payments_mock import MockPaymentsProvider
from core.lifecycle import LifecycleState as S
from core.recovery import DecisionKind as D
from storage import durable_lifecycle as lifecycle
from storage.database import local_database_url
from storage.experimental_durable_execution import ExecutionStatus as X, ExecutionTask, ExecutionWorker
from storage.experimental_recovery_worker import Reconciler
from storage.submission import submit_intent
from tests.integration.lifecycle_fixture import DisposableLifecycleDatabase

ROOT=Path.cwd()
OUT=Path('/tmp/safeact-pooling-experiment-20261024')
OUT.mkdir(mode=0o700,exist_ok=False)

def guard(event,args):
    if event=='socket.connect':
        address=args[1]
        if isinstance(address,tuple) and address[0] not in ('127.0.0.1','::1'):
            raise RuntimeError('Non-loopback connection forbidden')
sys.addaudithook(guard)

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def now():return time.perf_counter_ns()
def ms(start,end):return (end-start)/1_000_000
def save(name,value):(OUT/name).write_text(json.dumps(value,indent=2)+'\n')
def stats(values):
    values=sorted(values)
    assert values and all(math.isfinite(value) and value>=0 for value in values)
    return dict(count=len(values),mean=sum(values)/len(values),
        p50=values[math.ceil(.5*len(values))-1],p95=values[math.ceil(.95*len(values))-1],
        p99=values[math.ceil(.99*len(values))-1],maximum=values[-1])

MODES=(('fresh_default',False,5),('pooled_disabled',True,None),
       ('pooled_auto',True,5),('pooled_immediate',True,0))
manifest,_=verify(ROOT/'chaos/manifest_qual_v1.json')
ledger_path=ROOT/manifest['budget']['execution_ledger_dir']
assert not ledger_path.exists()
pin=sha(ROOT/'chaos/manifest_qual_v1.json')
source=sha(ROOT/'chaos/source_qual_v1.json')
save('protocol.json',dict(scope='Benchmark-only pooling adapter; production code unchanged',
    modes=[dict(name=name,pooled=pooled,prepare_threshold=threshold) for name,pooled,threshold in MODES],
    concurrency=[1,4],pool_capacity_per_role=4,max_overflow=0,
    warmups_per_configuration=32,measured_per_configuration=100,
    blocks=10,requests_per_block=10,
    ordering='Rotate configuration order by block; reverse on odd blocks; same provider and fixture',
    preparation='Fresh default=5; pooled disabled=None, automatic=5, immediate=0',
    connection_lifetime='Same benchmark adapter for every mode; check out native psycopg connection, execute unchanged _execute, return with rollback-on-return',
    fallback='Original worker observation/retry connections remain fresh and role-bound',
    timer='perf_counter_ns; same cursor and provider timing in every mode',
    percentiles='nearest rank; 100 samples/configuration, descriptive p99 only',
    exclusions=['Public API/authentication','Celery/Redis/relay scheduling','Startup/warmup/cleanup','Production capacity'],
    external_provider_calls=0,paid_calls=0,manifest_sha256=pin,source_sha256=source,
    script_sha256=sha(__file__),python=platform.python_version(),os=platform.system(),
    cpu_count=os.cpu_count(),load_start=list(os.getloadavg())))

class TimedProvider:
    def __init__(self,wire,row):self.wire,self.row=wire,row
    def refund(self,*args,**kwargs):
        started=now();self.row['provider_calls']+=1
        try:return self.wire.refund(*args,**kwargs)
        finally:self.row['provider_http_ms']+=ms(started,now())

class LeaseWorker(ExecutionWorker):
    """Prototype: only initial connection lifetime differs from the real worker."""
    def __init__(self,*,pool,expected_role,row=None,**kwargs):
        super().__init__(**kwargs)
        self.pool,self.expected_role,self.row=pool,expected_role,row
        self.owner_pid=os.getpid()
    def execute(self,task):
        if os.getpid()!=self.owner_pid:raise RuntimeError('Pool cannot cross a fork')
        started=now();lease=self.pool.connect();connection=lease.driver_connection
        original_cursor=connection.cursor_factory
        try:
            assert type(connection) is psycopg.Connection
            assert connection.info.user==self.expected_role
            assert connection.info.transaction_status is psycopg.pq.TransactionStatus.IDLE
            if self.row is not None:
                row=self.row
                row['worker_checkout_ms']=ms(started,now())
                row['worker_backend_pid']=connection.info.backend_pid
                class TimedCursor(psycopg.Cursor):
                    def execute(self,*args,**kwargs):
                        started=now()
                        try:return super().execute(*args,**kwargs)
                        finally:
                            row['worker_sql_ms']+=ms(started,now())
                            row['worker_sql_calls']+=1
                connection.cursor_factory=TimedCursor
            return self._execute(connection,task)
        finally:
            if connection.closed:
                lease.invalidate()
            else:
                connection.cursor_factory=original_cursor
            lease.close()

db=DisposableLifecycleDatabase()
ledger=EffectsLedger();servers=MockProviderServers(ledger=ledger)
resources={};client=None;rows=[];blocks=[];safety=[];prepared=[]
cleanup={}
try:
    db.start()
    for name,pooled,threshold in MODES:
        fresh_settings=dict(db.settings,user=db.worker_a,password=db.passwords[db.worker_a],prepare_threshold=threshold)
        def creator(settings=fresh_settings):return psycopg.connect(**settings)
        pool=QueuePool(creator,pool_size=4,max_overflow=0,timeout=10) if pooled else NullPool(creator)
        kwargs=dict(poolclass=QueuePool if pooled else NullPool,
            connect_args=dict(connect_timeout=5,options='-c search_path='+db.schema+',public',prepare_threshold=threshold))
        if pooled:kwargs.update(pool_size=4,max_overflow=0,pool_timeout=10)
        engine=sa.create_engine(local_database_url(),**kwargs)
        resources[name]=dict(pool=pool,engine=engine,creator=creator,pooled=pooled,threshold=threshold)
        # Hold four leases simultaneously: ensures real, bounded pre-warming.
        for owner in (pool,engine.pool):
            leases=[owner.connect() for _ in range(4)]
            try:
                for lease in leases:
                    raw=lease.driver_connection
                    expected=db.worker_a if owner is pool else db.settings['user']
                    assert raw.execute('SELECT session_user').fetchone()[0]==expected
                    raw.rollback()
            finally:
                for lease in leases:lease.close()

    # Validate ownership/reset/failure behavior in both prepared pooled modes.
    for mode in ('pooled_disabled','pooled_immediate'):
        resource=resources[mode];pool=resource['pool']
        provider_ledger=EffectsLedger();payment=MockPaymentsProvider(ledger=provider_ledger)
        email=MockEmailProvider(ledger=provider_ledger)
        connectors=[PaymentsConnector(payment),EmailConnector(email)]
        def admit(**kwargs):
            request=db.request(request_key='pool-safety-'+uuid4().hex,**kwargs)
            receipt=submit_intent(resource['engine'],request)
            assert receipt.created
            return receipt.operation_id
        def execute(operation,hook=None):
            return LeaseWorker(pool=pool,expected_role=db.worker_a,
                connect=resource['creator'],schema=db.schema,connectors=connectors,
                fault_hook=hook).execute(ExecutionTask(operation,uuid4(),uuid4()))
        def state(operation):return lifecycle.current_state(db.admin,db.schema,operation).state
        def record(name,**details):safety.append(dict(mode=mode,case=name,passed=True,**details))

        operation=admit()
        assert execute(operation).status is X.SUCCEEDED
        assert execute(operation).status is X.NOT_CLAIMABLE
        key=db.provider_key(operation)
        assert len(provider_ledger.calls_for(key))==len(provider_ledger.effects_for(key))==1
        record('duplicate_delivery_one_effect')
        for point,expected in (('after_claim_commit',S.CLAIMED),('after_fence_commit',S.DISPATCHING)):
            operation=admit()
            def lose_ack(where,_connection):
                if where==point:raise ConnectionError('synthetic lost commit acknowledgement')
            assert execute(operation,lose_ack).status is X.UNCERTAIN
            assert state(operation) is expected
            assert not provider_ledger.calls_for(db.provider_key(operation))
            record(point+'_blocks_send')
        operation=admit()
        def outcome_ack(where,_connection):
            if where=='after_outcome_commit':raise ConnectionError('synthetic lost outcome acknowledgement')
        result=execute(operation,outcome_ack)
        assert result.status is X.SUCCEEDED and result.stage=='observed'
        assert execute(operation).status is X.NOT_CLAIMABLE
        key=db.provider_key(operation)
        assert len(provider_ledger.calls_for(key))==len(provider_ledger.effects_for(key))==1
        record('lost_outcome_ack_observes_without_resend')
        operation=admit()
        def close_before_outcome(where,connection):
            if where=='before_outcome_commit':connection.close()
        assert execute(operation,close_before_outcome).status is X.SUCCEEDED
        key=db.provider_key(operation)
        assert len(provider_ledger.calls_for(key))==len(provider_ledger.effects_for(key))==1
        assert execute(admit()).status is X.SUCCEEDED
        record('broken_connection_discarded_and_replaced')
        lease=pool.connect();raw=lease.driver_connection
        raw.execute("SELECT set_config('application_name','pool-reset-check',true)")
        lease.close()
        leases=[pool.connect() for _ in range(4)]
        try:
            for lease in leases:
                raw=lease.driver_connection
                assert raw.info.transaction_status is psycopg.pq.TransactionStatus.IDLE
                assert raw.execute('SELECT session_user').fetchone()[0]==db.worker_a
                assert raw.execute('SHOW application_name').fetchone()[0]!='pool-reset-check'
                raw.rollback()
        finally:
            for lease in leases:lease.close()
        record('transaction_local_state_reset_and_role_preserved')
        worker=LeaseWorker(pool=pool,expected_role=db.worker_a,
            connect=resource['creator'],schema=db.schema,connectors=connectors)
        worker.owner_pid=-1
        try:worker.execute(ExecutionTask(uuid4(),uuid4(),uuid4()))
        except RuntimeError:pass
        else:raise AssertionError('Fork guard accepted foreign process')
        record('foreign_process_refused_before_checkout')
        operation=admit(connector_id='mock-email',action_type='send_refund_confirmation',
            resource_id='pool-email',provider_idempotency_key=None,
            payload=dict(to='customer@example.invalid',template='refund'))
        email.fail_after_effect=True
        assert execute(operation).status is X.UNKNOWN
        with db.connect(db.recoverer) as connection:
            reconciler=Reconciler(connect=lambda:None,schema=db.schema,connectors=connectors,retry_authorized=True)
            result=reconciler.reconcile(operation,connection)
            assert result.decision is D.ESCALATE_UNRESOLVED
            assert reconciler.reconcile(operation,connection).status.value=='skipped'
        assert state(operation) is S.ESCALATED_UNRESOLVED
        assert len(provider_ledger.calls_for(str(operation)))==len(provider_ledger.effects_for(str(operation)))==1
        record('ambiguous_non_idempotent_email_escalates_without_resend')
    save('pool-safety-checks.json',safety)
    assert len(safety)==16
    print('16 pool ownership/reset/recovery checks passed',flush=True)

    servers.start()
    client=httpx.Client(base_url=servers.url('payments'),trust_env=False,follow_redirects=False)
    wire=HttpPaymentsProvider(servers.url('payments'),client=client)
    def request(mode,concurrency,warmup,index):
        resource=resources[mode];identity='pool-perf-'+uuid4().hex
        payload=dict(charge_id=identity,amount_minor=100)
        row=dict(mode=mode,concurrency=concurrency,warmup=warmup,index=index,resource_id=identity,
            provider_calls=0,provider_http_ms=0.0,worker_sql_ms=0.0,worker_sql_calls=0,
            worker_checkout_ms=0.0,commit_ms={})
        timed=TimedProvider(wire,row);commit_start={}
        def hook(point,_connection):
            if point.startswith('before_') and point.endswith('_commit'):commit_start[point[7:-7]]=now()
            elif point.startswith('after_') and point.endswith('_commit'):
                stage=point[6:-7];row['commit_ms'][stage]=ms(commit_start[stage],now())
        started=now()
        receipt=submit_intent(resource['engine'],db.request(request_key=identity,resource_id=identity,payload=payload))
        assert receipt.created
        row['admission_ms']=ms(started,now());row['operation_id']=str(receipt.operation_id)
        worker=LeaseWorker(pool=resource['pool'],expected_role=db.worker_a,row=row,
            connect=resource['creator'],schema=db.schema,connectors=[PaymentsConnector(timed)],fault_hook=hook)
        begin=now();result=worker.execute(ExecutionTask(receipt.operation_id,uuid4(),uuid4()))
        row['worker_ms']=ms(begin,now());row['total_ms']=ms(started,now())
        assert result.status is X.SUCCEEDED and result.provider_called
        assert set(row['commit_ms'])=={'claim','fence','outcome'}
        assert row['provider_calls']==1 and row['worker_sql_calls']==5
        row['worker_commit_ms']=sum(row['commit_ms'].values())
        row['non_provider_ms']=row['total_ms']-row['provider_http_ms']
        return row

    for concurrency in (1,4):
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            for mode,_,_ in MODES:
                rows.extend(executor.map(lambda i:request(mode,concurrency,True,i),range(32)))
            for block in range(10):
                order=[mode[0] for mode in MODES]
                offset=block%len(order);order=order[offset:]+order[:offset]
                if block%2:order.reverse()
                for mode in order:
                    started=now()
                    results=list(executor.map(lambda i:request(mode,concurrency,False,block*10+i),range(10)))
                    blocks.append(dict(mode=mode,concurrency=concurrency,block=block,
                        operations=10,elapsed_seconds=ms(started,now())/1000))
                    rows.extend(results)
                print('concurrency='+str(concurrency),'block='+str(block),'complete',flush=True)
    assert len(rows)==1056 and sum(not row['warmup'] for row in rows)==800
    calls,effects=ledger.snapshot()
    assert len(calls)==len(effects)==1056
    resources_by_id={params['charge_id']:key for key,params in calls}
    assert len(resources_by_id)==1056
    for row in rows:
        key=resources_by_id[row['resource_id']]
        assert len(ledger.calls_for(key))==len(ledger.effects_for(key))==1
        assert state(__import__('uuid').UUID(row['operation_id'])) is S.SUCCEEDED
        row['observed_calls']=row['observed_effects']=1
        row['correlation_sha256']=correlation_hash(key)
    for mode,_,_ in MODES:
        for owner,label in ((resources[mode]['pool'],'worker'),(resources[mode]['engine'].pool,'admission')):
            leases=[owner.connect() for _ in range(4)]
            try:
                for lease in leases:
                    raw=lease.driver_connection
                    count=raw.execute('SELECT count(*) FROM pg_prepared_statements').fetchone()[0]
                    role=raw.execute('SELECT session_user').fetchone()[0];raw.rollback()
                    prepared.append(dict(mode=mode,role=label,backend_pid=raw.info.backend_pid,
                        prepared_statement_count=count,role_matches=role==(db.worker_a if label=='worker' else db.settings['user'])))
            finally:
                for lease in leases:lease.close()
    summary=[]
    for concurrency in (1,4):
        for mode,_,_ in MODES:
            selected=[row for row in rows if row['mode']==mode and row['concurrency']==concurrency and not row['warmup']]
            assert len(selected)==100
            elapsed=sum(block['elapsed_seconds'] for block in blocks if block['mode']==mode and block['concurrency']==concurrency)
            summary.append(dict(mode=mode,concurrency=concurrency,measured_operations=100,
                requests_per_second=100/elapsed,
                distinct_worker_backends=len({row['worker_backend_pid'] for row in selected}),
                metrics={field:stats([row[field] for row in selected]) for field in (
                    'total_ms','admission_ms','worker_ms','worker_checkout_ms','worker_sql_ms',
                    'worker_commit_ms','provider_http_ms','non_provider_ms')}))
    (OUT/'samples.jsonl').write_text('\n'.join(json.dumps(row) for row in rows)+'\n')
    save('blocks.json',blocks);save('provider-ledger.json',ledger.events())
    save('prepared-statements.json',prepared)
    save('summary.json',dict(configurations=summary,measured_operations=800,warmup_operations=256,
        provider_calls=1056,provider_effects=1056,errors=0,duplicate_effects=0,
        safety_checks=len(safety),production_code_changed=False,load_end=list(os.getloadavg())))
finally:
    if client is not None:client.close()
    servers.stop();cleanup['mock_threads_stopped']=all(not thread.is_alive() for thread in servers._threads)
    for resource in resources.values():resource['engine'].dispose();resource['pool'].dispose()
    db.cleanup()
    with psycopg.connect(**db.settings,autocommit=True) as connection:
        cleanup['schema_removed']=connection.execute('SELECT NOT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)',(db.schema,)).fetchone()[0]
        cleanup['roles_removed']=connection.execute('SELECT NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=ANY(%s))',(db._created_roles,)).fetchone()[0]
    verify(ROOT/'chaos/manifest_qual_v1.json')
    cleanup['frozen_inputs_unchanged']=pin==sha(ROOT/'chaos/manifest_qual_v1.json') and source==sha(ROOT/'chaos/source_qual_v1.json')
    cleanup['frozen_ledger_absent']=not ledger_path.exists()
    save('cleanup.json',cleanup)
    assert all(cleanup.values()),cleanup
print('Pooling/preparation comparison complete; 800 measured operations; 16 safety checks; no qualification executed',flush=True)
