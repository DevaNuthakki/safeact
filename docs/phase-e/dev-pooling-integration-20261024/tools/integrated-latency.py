"""Local comparison through the integrated ExecutionWorker.execute path.

No subclass or replacement execution policy; no qualification invocation.
"""
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing,contextmanager
from pathlib import Path
from uuid import UUID,uuid4

import httpx2 as httpx
import psycopg
import sqlalchemy as sa
from psycopg import sql

from chaos.frozen import ROOT,verify,sha
from connectors.http_mock.servers import MockProviderServers
from connectors.http_providers import HttpPaymentsProvider
from connectors.http_trace import correlation_hash
from connectors.ledger import EffectsLedger
from connectors.payments import PaymentsConnector
from storage.connection_pool import ConnectionPool,reset_engine_sessions
from storage.database import local_database_url
from storage.experimental_durable_execution import ExecutionStatus as X,ExecutionTask,ExecutionWorker
from storage.submission import submit_intent
from tests.integration.lifecycle_fixture import DisposableLifecycleDatabase

OUT=Path('/tmp/safeact-pool-integration-20261024/latency');OUT.mkdir(mode=0o700,exist_ok=False)
def guard(event,args):
    if event=='socket.connect' and isinstance(args[1],tuple) and args[1][0] not in ('127.0.0.1','::1'):
        raise RuntimeError('Only loopback connections allowed')
sys.addaudithook(guard)
def now():return time.perf_counter_ns()
def ms(start,end):return (end-start)/1_000_000
def save(name,value):(OUT/name).write_text(json.dumps(value,indent=2)+'\n')
def distribution(values):
    values=sorted(values)
    assert values and all(math.isfinite(value) and value>=0 for value in values)
    return dict(count=len(values),mean=sum(values)/len(values),
        p50=values[math.ceil(.5*len(values))-1],p95=values[math.ceil(.95*len(values))-1],
        p99=values[math.ceil(.99*len(values))-1],maximum=values[-1])
manifest,_=verify(ROOT/'chaos/manifest_qual_v1.json')
assert not (ROOT/manifest['budget']['execution_ledger_dir']).exists()
pin=sha((ROOT/'chaos/manifest_qual_v1.json').read_bytes())
save('protocol.json',dict(scope='Bounded integrated worker check only; no frozen execution',
    modes=['fresh_connections','integrated_pool'],concurrency=[1,4],
    measured_per_configuration=100,warmups_per_configuration=32,blocks=10,operations_per_block=10,
    order='Alternate mode order across blocks; same provider, amount, client, unique resources and identities',
    path='submit_intent then unchanged ExecutionWorker.execute with fresh or integrated connection_scope',
    pool='ConnectionPool native leases; preflight liveness and full session reset included in measured time',
    admission='Fresh fixture NullPool versus bounded QueuePool with reset_engine_sessions',
    prepare_threshold='Psycopg default automatic preparation in both paths',
    timer='perf_counter_ns; nearest-rank percentiles',
    exclusions=['Public API/authentication','Celery/Redis/relay scheduling','Setup/warmup/cleanup','Production capacity'],
    manifest_sha256=pin,script_sha256=sha(Path(__file__).read_bytes()),load_start=list(os.getloadavg()),
    external_provider_calls=0,paid_calls=0))

class TimedProvider:
    def __init__(self,wire,row):self.wire,self.row=wire,row
    def refund(self,*args,**kwargs):
        started=now();self.row['provider_calls']+=1
        try:return self.wire.refund(*args,**kwargs)
        finally:self.row['provider_http_ms']+=ms(started,now())

db=DisposableLifecycleDatabase();ledger=EffectsLedger();servers=MockProviderServers(ledger=ledger)
owner=engine=client=None;rows=[];blocks=[];cleanup={}
try:
    db.start()
    owner=ConnectionPool(dict(db.settings,user=db.worker_a,password=db.passwords[db.worker_a]),size=4)
    engine=reset_engine_sessions(sa.create_engine(local_database_url(),pool_size=4,max_overflow=0,pool_timeout=5,
        connect_args=dict(options='-c search_path='+db.schema+',public')))
    servers.start()
    client=httpx.Client(base_url=servers.url('payments'),trust_env=False,follow_redirects=False)
    wire=HttpPaymentsProvider(servers.url('payments'),client=client)
    def request(mode,concurrency,warmup,index):
        identity='integrated-latency-'+uuid4().hex
        row=dict(mode=mode,concurrency=concurrency,warmup=warmup,index=index,resource_id=identity,
            provider_calls=0,provider_http_ms=0.0,worker_checkout_ms=0.0,worker_sql_ms=0.0,
            worker_sql_calls=0,commit_ms={})
        @contextmanager
        def scope():
            started=now()
            context=owner.connection() if mode=='integrated_pool' else closing(db.connect(db.worker_a))
            with context as connection:
                row['worker_checkout_ms']=ms(started,now())
                row['backend_pid']=connection.info.backend_pid
                original=connection.cursor_factory
                class Cursor(psycopg.Cursor):
                    def execute(self,*args,**kwargs):
                        begin=now()
                        try:return super().execute(*args,**kwargs)
                        finally:
                            row['worker_sql_ms']+=ms(begin,now());row['worker_sql_calls']+=1
                connection.cursor_factory=Cursor
                try:yield connection
                finally:
                    if not connection.closed:connection.cursor_factory=original
        begin_commit={}
        def hook(point,_connection):
            if point.startswith('before_') and point.endswith('_commit'):begin_commit[point[7:-7]]=now()
            elif point.startswith('after_') and point.endswith('_commit'):
                stage=point[6:-7];row['commit_ms'][stage]=ms(begin_commit[stage],now())
        started=now()
        receipt=submit_intent(
            engine if mode=='integrated_pool' else db.engine,
            db.request(request_key=identity,resource_id=identity,payload=dict(charge_id=identity,amount_minor=100)))
        row['admission_ms']=ms(started,now());row['operation_id']=str(receipt.operation_id)
        assert receipt.created
        worker=ExecutionWorker(connect=lambda:db.connect(db.worker_a),connection_scope=scope,
            schema=db.schema,connectors=[PaymentsConnector(TimedProvider(wire,row))],fault_hook=hook)
        begin=now();answer=worker.execute(ExecutionTask(receipt.operation_id,uuid4(),uuid4()))
        row['worker_ms']=ms(begin,now());row['total_ms']=ms(started,now())
        assert answer.status is X.SUCCEEDED and answer.provider_called
        assert set(row['commit_ms'])=={'claim','fence','outcome'}
        assert row['provider_calls']==1 and row['worker_sql_calls']==5
        row['worker_commit_ms']=sum(row['commit_ms'].values())
        row['non_provider_ms']=row['total_ms']-row['provider_http_ms']
        return row
    for concurrency in (1,4):
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            for mode in ('fresh_connections','integrated_pool'):
                rows.extend(executor.map(lambda i:request(mode,concurrency,True,i),range(32)))
            for block in range(10):
                order=('fresh_connections','integrated_pool') if block%2==0 else ('integrated_pool','fresh_connections')
                for mode in order:
                    started=now()
                    rows.extend(executor.map(lambda i:request(mode,concurrency,False,block*10+i),range(10)))
                    blocks.append(dict(mode=mode,concurrency=concurrency,block=block,operations=10,
                        elapsed_seconds=ms(started,now())/1000))
                print('concurrency='+str(concurrency),'block='+str(block),'complete',flush=True)
    assert len(rows)==528 and sum(not row['warmup'] for row in rows)==400
    calls,effects=ledger.snapshot();assert len(calls)==len(effects)==528
    keys={params['charge_id']:key for key,params in calls};assert len(keys)==528
    for row in rows:
        key=keys[row['resource_id']]
        assert len(ledger.calls_for(key))==len(ledger.effects_for(key))==1
        state=db.admin.execute(sql.SQL('SELECT state::text FROM {} WHERE operation_id=%s').format(
            db.table('safeact_operation_current_state')),(UUID(row['operation_id']),)).fetchone()[0]
        assert state=='SUCCEEDED'
        row.update(committed_state=state,observed_calls=1,observed_effects=1,correlation_sha256=correlation_hash(key))
    summary=[]
    for concurrency in (1,4):
        for mode in ('fresh_connections','integrated_pool'):
            selected=[row for row in rows if row['mode']==mode and row['concurrency']==concurrency and not row['warmup']]
            assert len(selected)==100
            elapsed=sum(row['elapsed_seconds'] for row in blocks if row['mode']==mode and row['concurrency']==concurrency)
            summary.append(dict(mode=mode,concurrency=concurrency,operations=100,requests_per_second=100/elapsed,
                distinct_worker_backends=len({row['backend_pid'] for row in selected}),
                metrics={key:distribution([row[key] for row in selected]) for key in (
                    'total_ms','admission_ms','worker_ms','worker_checkout_ms','worker_sql_ms',
                    'worker_commit_ms','provider_http_ms','non_provider_ms')}))
    save('summary.json',dict(configurations=summary,measured_operations=400,warmups=128,
        errors=0,duplicate_effects=0,provider_calls=528,provider_effects=528,load_end=list(os.getloadavg())))
    save('blocks.json',blocks);save('provider-ledger.json',ledger.events())
    (OUT/'samples.jsonl').write_text('\n'.join(json.dumps(row) for row in rows)+'\n')
finally:
    if client is not None:client.close()
    servers.stop()
    if owner is not None:owner.close()
    if engine is not None:engine.dispose()
    db.cleanup()
    with psycopg.connect(**db.settings,autocommit=True) as connection:
        cleanup['schema_removed']=connection.execute('SELECT NOT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)',(db.schema,)).fetchone()[0]
        cleanup['roles_removed']=connection.execute('SELECT NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=ANY(%s))',(db._created_roles,)).fetchone()[0]
    cleanup['mock_threads_stopped']=all(not thread.is_alive() for thread in servers._threads)
    verify(ROOT/'chaos/manifest_qual_v1.json')
    cleanup['source_pin_unchanged']=pin==sha((ROOT/'chaos/manifest_qual_v1.json').read_bytes())
    cleanup['frozen_ledger_absent']=not (ROOT/manifest['budget']['execution_ledger_dir']).exists()
    save('cleanup.json',cleanup);assert all(cleanup.values()),cleanup
print('Integrated latency check complete; 400 measured operations, no errors/duplicates; resources removed',flush=True)
