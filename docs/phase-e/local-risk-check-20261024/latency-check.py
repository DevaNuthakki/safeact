"""Bounded loopback HTTP/SQL timing check, entirely outside frozen qualification.

Fixed before measurement: 100 measured requests per path at concurrency 1 and
4, five warmups per path/concurrency. Ten balanced blocks alternate path order.
Direct HTTP is a lower-bound reference, not an equivalent durable baseline.
"""
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
from psycopg import sql

from chaos.frozen import verify
from connectors.http_mock.servers import MockProviderServers
from connectors.http_providers import HttpPaymentsProvider
from connectors.http_trace import correlation_hash
from connectors.ledger import EffectsLedger
from storage.experimental_durable_execution import ExecutionStatus, ExecutionTask, ExecutionWorker
from tests.integration.lifecycle_fixture import DisposableLifecycleDatabase

ROOT = Path.cwd()
OUT = Path('/tmp/safeact-local-risk-check-20261024')
OUT.mkdir(mode=0o700, exist_ok=True)

def network_guard(event, args):
    if event == 'socket.connect':
        address = args[1]
        if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1'):
            raise RuntimeError('Only loopback network connections allowed')

sys.addaudithook(network_guard)

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def stamp():
    return time.perf_counter_ns()

def ms(start, end):
    return (end-start)/1_000_000

def save(name, value):
    (OUT/name).write_text(json.dumps(value, indent=2)+'\n')

manifest, _ = verify(ROOT/'chaos/manifest_qual_v1.json')
ledger_path = ROOT/manifest['budget']['execution_ledger_dir']
assert not ledger_path.exists(), 'Frozen ledger must remain absent'
pin_before = sha(ROOT/'chaos/manifest_qual_v1.json')
source_before = sha(ROOT/'chaos/source_qual_v1.json')
save('benchmark-protocol.json', dict(
    scope='Local worker microbenchmark; no frozen scenarios, queue runner, smoke or calibration',
    paths=['direct_http', 'safeact_durable_worker'], concurrency=[1,4],
    measured_per_configuration=100, warmups_per_configuration=5,
    blocks_per_configuration=10, requests_per_block=10,
    ordering='Path order alternates per block; same shared loopback provider and HTTP client',
    timer='perf_counter_ns', percentile_method='nearest rank',
    provider='mock payments refund; same amount, unique resource and request identity',
    durable_path='submit_intent with NullPool, then ExecutionWorker.execute synchronously',
    excluded=['Application HTTP admission/authentication', 'Redis/Celery queue scheduling',
              'Toxiproxy/fault injection', 'Startup/migrations/warmup/cleanup',
              'Production load or sustained saturation'],
    limitations=['Direct HTTP provides no durable guarantees and is not B0/B1/B2/B3',
                 'No acceptance SLO specified; report observations without pass/fail latency threshold',
                 '100 samples/configuration cannot establish stable tail latency',
                 'Non-provider overhead includes admission, connections, SQL, commits and Python processing'],
    manifest_sha256=pin_before, source_sha256=source_before,
    machine=dict(os=platform.system(), release=platform.release(), architecture=platform.machine(),
                 python=platform.python_version(), cpu_count=os.cpu_count(), load_start=list(os.getloadavg())),
    external_api_calls=0, paid_calls=0))

class TimedProvider:
    def __init__(self, wire, row):
        self.wire, self.row = wire, row

    def refund(self, *args, **kwargs):
        started = stamp()
        self.row['provider_calls'] += 1
        try:
            return self.wire.refund(*args, **kwargs)
        finally:
            self.row['provider_http_ms'] += ms(started, stamp())

def distribution(values):
    values = sorted(values)
    assert values and all(math.isfinite(value) and value >= 0 for value in values)
    return dict(count=len(values), mean=sum(values)/len(values),
        p50=values[math.ceil(.5*len(values))-1], p95=values[math.ceil(.95*len(values))-1],
        p99=values[math.ceil(.99*len(values))-1], minimum=values[0], maximum=values[-1])

db = DisposableLifecycleDatabase()
ledger = EffectsLedger()
servers = MockProviderServers(ledger=ledger)
client = None
rows, blocks = [], []
cleanup = dict(database_removed=False, roles_removed=False, providers_stopped=False,
               frozen_ledger_absent=False, pin_unchanged=False)
try:
    db.start()
    servers.start()
    client = httpx.Client(base_url=servers.url('payments'), trust_env=False, follow_redirects=False)
    wire = HttpPaymentsProvider(servers.url('payments'), client=client)
    from connectors.payments import PaymentsConnector

    def request(mode, concurrency, warmup, index):
        identity = 'local-risk-'+uuid4().hex
        payload = dict(charge_id=identity, amount_minor=100)
        row = dict(path=mode, concurrency=concurrency, warmup=warmup, index=index,
            resource_id=identity, provider_calls=0, provider_http_ms=0.0,
            worker_sql_execute_ms=0.0, worker_sql_execute_count=0,
            worker_connection_ms=0.0, commit_ms={})
        timed = TimedProvider(wire, row)
        started = stamp()
        if mode == 'direct_http':
            key = 'direct-'+uuid4().hex
            receipt = timed.refund(key, payload, timeout=5)
            row['outcome'] = 'confirmed_success'
            row['provider_replayed'] = receipt.replayed
            row['admission_ms'] = row['worker_ms'] = 0.0
        else:
            admission_start = stamp()
            operation_id = db.admit(request_key=identity, resource_id=identity, payload=payload)
            admission_end = stamp()
            row['operation_id'] = str(operation_id)
            row['admission_ms'] = ms(admission_start, admission_end)
            commit_start = {}

            def hook(point, _connection):
                if point.startswith('before_') and point.endswith('_commit'):
                    commit_start[point[7:-7]] = stamp()
                elif point.startswith('after_') and point.endswith('_commit'):
                    stage = point[6:-7]
                    row['commit_ms'][stage] = ms(commit_start[stage], stamp())

            def connect():
                begin = stamp()
                connection = db.connect(db.worker_a)
                row['worker_connection_ms'] += ms(begin, stamp())
                # Preserve the strict native psycopg.Connection type required
                # by the lifecycle service; instrument only its cursor factory.
                class TimedCursor(psycopg.Cursor):
                    def execute(self, *args, **kwargs):
                        started = stamp()
                        try:
                            return super().execute(*args, **kwargs)
                        finally:
                            row['worker_sql_execute_ms'] += ms(started, stamp())
                            row['worker_sql_execute_count'] += 1
                connection.cursor_factory = TimedCursor
                return connection

            worker = ExecutionWorker(connect=connect, schema=db.schema,
                connectors=[PaymentsConnector(timed)], fault_hook=hook)
            worker_start = stamp()
            result = worker.execute(ExecutionTask(operation_id, uuid4(), uuid4()))
            row['worker_ms'] = ms(worker_start, stamp())
            assert result.status is ExecutionStatus.SUCCEEDED and result.provider_called
            assert set(row['commit_ms']) == {'claim','fence','outcome'}
            row['outcome'] = result.status.value
        finished = stamp()
        row['total_ms'] = ms(started, finished)
        row['non_provider_ms'] = row['total_ms']-row['provider_http_ms']
        row['worker_commit_ms'] = sum(row['commit_ms'].values())
        assert row['provider_calls'] == 1
        return row

    for concurrency in (1,4):
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for mode in ('direct_http','safeact_durable_worker'):
                rows.extend(pool.map(lambda i: request(mode, concurrency, True, i), range(5)))
            for block in range(10):
                modes = ('direct_http','safeact_durable_worker') if block%2 == 0 else ('safeact_durable_worker','direct_http')
                for mode in modes:
                    started = stamp()
                    result = list(pool.map(lambda i: request(mode, concurrency, False, block*10+i), range(10)))
                    elapsed = ms(started, stamp())/1000
                    rows.extend(result)
                    blocks.append(dict(path=mode, concurrency=concurrency, block=block,
                                       requests=10, elapsed_seconds=elapsed))
                    print('concurrency='+str(concurrency), 'block='+str(block), mode, 'complete', flush=True)
    assert len(rows) == 420
    calls, effects = ledger.snapshot()
    assert len(calls) == len(effects) == 420
    by_resource = {params['charge_id']: key for key, params in calls}
    assert len(by_resource) == 420
    for row in rows:
        key = by_resource[row['resource_id']]
        assert len(ledger.calls_for(key)) == len(ledger.effects_for(key)) == 1
        row['observed_calls'] = row['observed_effects'] = 1
        row['correlation_sha256'] = correlation_hash(key)
        if row['path'] == 'safeact_durable_worker':
            state = db.admin.execute(sql.SQL(
                'SELECT state::text FROM {} WHERE operation_id = %s'
            ).format(db.table('safeact_operation_current_state')), (row['operation_id'],)).fetchone()[0]
            assert state == 'SUCCEEDED'
            row['committed_state'] = state
    summary = []
    for concurrency in (1,4):
        for mode in ('direct_http','safeact_durable_worker'):
            chosen = [row for row in rows if row['path']==mode and row['concurrency']==concurrency and not row['warmup']]
            assert len(chosen)==100
            elapsed = sum(block['elapsed_seconds'] for block in blocks if block['path']==mode and block['concurrency']==concurrency)
            metrics = {field: distribution([row[field] for row in chosen]) for field in (
                'total_ms','admission_ms','worker_ms','provider_http_ms','non_provider_ms',
                'worker_connection_ms','worker_sql_execute_ms','worker_commit_ms')}
            if mode=='safeact_durable_worker':
                metrics['commit_by_stage_ms'] = {stage:distribution([row['commit_ms'][stage] for row in chosen])
                    for stage in ('claim','fence','outcome')}
            summary.append(dict(path=mode, concurrency=concurrency, measured_requests=100,
                observed_provider_calls=100, observed_provider_effects=100, errors=0,
                completed_requests_per_second=100/elapsed, metrics=metrics))
    (OUT/'latency-samples.jsonl').write_text('\n'.join(json.dumps(row) for row in rows)+'\n')
    save('latency-blocks.json', blocks)
    save('provider-ledger.json', ledger.events())
    save('latency-summary.json', dict(scope='Bounded local worker/HTTP microbenchmark; not qualification or production capacity',
        configurations=summary, total_requests_including_warmup=420, measured_requests=400,
        errors=0, prohibited_duplicate_effects=0, load_end=list(os.getloadavg()),
        sql_timing_note='Worker execute spans cover protected SQL calls; admission total also includes its SQL and connections. Non-provider time is not exclusively SQL.',
        external_calls=0, paid_calls=0, frozen_scopes_executed=[]))
finally:
    if client is not None:
        client.close()
    servers.stop()
    cleanup['providers_stopped'] = all(not thread.is_alive() for thread in servers._threads)
    db.cleanup()
    with psycopg.connect(**db.settings, autocommit=True) as checker:
        cleanup['database_removed'] = checker.execute('SELECT NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s)', (db.schema,)).fetchone()[0]
        cleanup['roles_removed'] = checker.execute('SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = ANY(%s))', (db._created_roles,)).fetchone()[0]
    verify(ROOT/'chaos/manifest_qual_v1.json')
    cleanup['pin_unchanged'] = sha(ROOT/'chaos/manifest_qual_v1.json') == pin_before and sha(ROOT/'chaos/source_qual_v1.json') == source_before
    cleanup['frozen_ledger_absent'] = not ledger_path.exists()
    save('cleanup.json', cleanup)
    assert all(cleanup.values()), cleanup

print('Measured 400 requests plus 20 warmups; no duplicates; disposable resources removed; frozen inputs untouched', flush=True)
