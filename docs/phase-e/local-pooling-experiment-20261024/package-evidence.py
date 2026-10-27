"""Verify and preserve an already-completed local pooling experiment."""
import hashlib
import json
import math
import shutil
import subprocess
from importlib.metadata import version
from pathlib import Path

from chaos.frozen import verify
from deploy.evaluation.agent_v1.secret_scan import scan

ROOT=Path.cwd();PRIVATE=Path('/tmp/safeact-pooling-experiment-20261024')
OUT=ROOT/'docs/phase-e/local-pooling-experiment-20261024'
OUT.mkdir(exist_ok=False)
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read(name):return json.loads((PRIVATE/name).read_text())
def write(name,value):(OUT/name).write_text(json.dumps(value,indent=2)+'\n')
protocol=read('protocol.json');summary=read('summary.json');safety=read('pool-safety-checks.json')
rows=[json.loads(line) for line in (PRIVATE/'samples.jsonl').read_text().splitlines()]
assert len(rows)==1056 and len({row['resource_id'] for row in rows})==1056
assert sum(not row['warmup'] for row in rows)==800
assert len(safety)==16 and all(row['passed'] for row in safety)
assert len({(row['mode'],row['case']) for row in safety})==16
assert summary['errors']==summary['duplicate_effects']==0
assert summary['provider_calls']==summary['provider_effects']==1056
assert all(row['provider_calls']==row['observed_calls']==row['observed_effects']==1 for row in rows)
events=read('provider-ledger.json')
calls=[row for row in events if row['event']=='call'];effects=[row for row in events if row['event']=='effect']
assert len(calls)==len(effects)==1056
assert {row['correlation_sha256'] for row in calls}=={row['correlation_sha256'] for row in rows}
assert {row['correlation_sha256'] for row in effects}=={row['correlation_sha256'] for row in rows}
assert len({row['effect_id'] for row in effects})==1056
for config in summary['configurations']:
    selected=[row for row in rows if not row['warmup'] and row['mode']==config['mode'] and row['concurrency']==config['concurrency']]
    assert len(selected)==100
    for name,value in config['metrics'].items():
        values=sorted(row[name] for row in selected)
        assert all(math.isfinite(value) and value>=0 for value in values)
        for label,fraction in (('p50',.5),('p95',.95),('p99',.99)):
            assert value[label]==values[math.ceil(fraction*len(values))-1]
        assert math.isclose(value['mean'],sum(values)/len(values),rel_tol=1e-12)
    assert config['distinct_worker_backends']==(100 if config['mode']=='fresh_default' else 4)
for row in rows:
    assert set(row['commit_ms'])=={'claim','fence','outcome'} and row['worker_sql_calls']==5
    assert math.isclose(row['worker_commit_ms'],sum(row['commit_ms'].values()),rel_tol=1e-12)
assert all(read('cleanup.json').values())
assert all(row['role_matches'] for row in read('prepared-statements.json'))
for row in read('prepared-statements.json'):
    assert (row['prepared_statement_count']==0)==(row['mode'] in ('fresh_default','pooled_disabled'))
assert sha('/tmp/safeact-pooling-experiment-20261024.py')==protocol['script_sha256']
manifest,_=verify(ROOT/'chaos/manifest_qual_v1.json')
assert not (ROOT/manifest['budget']['execution_ledger_dir']).exists()
assert sha(ROOT/'chaos/manifest_qual_v1.json')==protocol['manifest_sha256']
assert sha(ROOT/'chaos/source_qual_v1.json')==protocol['source_sha256']
for path in PRIVATE.glob('*.json*'):shutil.copyfile(path,OUT/path.name)
shutil.copyfile('/tmp/safeact-pooling-experiment-20261024.py',OUT/'experiment.py')
shutil.copyfile('/tmp/safeact-package-pooling-experiment-20261024.py',OUT/'package-evidence.py')
shutil.copyfile('/tmp/safeact-pooling-experiment-20261024.log',OUT/'execution.log')
shutil.copyfile(ROOT/'chaos/source_qual_v1.json',OUT/'source-sha256.json')
older=json.loads((ROOT/'docs/phase-e/local-risk-check-20261024/unchanged-packets.json').read_text())
older.append(dict(path='docs/phase-e/local-risk-check-20261024',artifacts=17))
unchanged=[]
for row in older:
    directory=ROOT/row['path'];metadata=json.loads((directory/'artifact-sha256.json').read_text())
    entries=metadata if isinstance(metadata,list) else metadata['files']
    assert len(entries)==row['artifacts']
    for entry in entries:assert sha(directory/entry['path'])==entry['sha256']
    unchanged.append(dict(path=row['path'],artifacts=len(entries),artifact_manifest_sha256=sha(directory/'artifact-sha256.json')))
assert len(unchanged)==11 and sum(row['artifacts'] for row in unchanged)==504
write('unchanged-packets.json',unchanged)
assert not subprocess.check_output(['git','diff','--cached','--name-only'])
head=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
assert head=='32693852af7f2deda9adf38c5803df26fec637f3'
subprocess.run(['git','diff','--check'],check=True)
write('verification.json',dict(scope='Pooling/preparation local experiment only',
    measured_operations=800,warmups=256,safety_checks=16,errors=0,duplicate_effects=0,
    production_code_changed=False,prototype_enabled_in_runtime=False,
    frozen_inputs_unchanged=True,frozen_execution_ledger_absent=True,
    pre_run_executed=False,calibration_executed=False,big_run_executed=False,
    external_provider_calls=0,paid_calls=0,commits_created=0,index_empty=True,base_commit=head,
    dependencies={'psycopg':version('psycopg'),'SQLAlchemy':version('SQLAlchemy')},
    older_packets_unchanged=11,older_artifacts_unchanged=504,
    manifest_sha256=protocol['manifest_sha256']))

table=[]
for concurrency in (1,4):
    for mode,label in (('fresh_default','Fresh connections, current preparation default'),
                       ('pooled_disabled','Pooled, preparation disabled'),
                       ('pooled_auto','Pooled, automatic preparation'),
                       ('pooled_immediate','Pooled, immediate preparation')):
        c=next(row for row in summary['configurations'] if row['mode']==mode and row['concurrency']==concurrency)
        metrics=c['metrics']['total_ms']
        table.append(f'| {concurrency} | {label} | {metrics["p50"]:.2f} | {metrics["p95"]:.2f} | {c["requests_per_second"]:.2f} |')

(OUT/'RESULTS.md').write_text('''# Local pooling and prepared-statement experiment — 2026-10-24

**Pooling substantially reduced latency in this local comparison. Forcing
immediate prepared statements showed no clear advantage over the driver's
automatic preparation. Sub-10-ms operation latency was not achieved.**

The user authorized the comparison and requested notice before pre-run work.
No pre-run, frozen smoke, calibration or big run executed. No real Stripe or
paid API requests occurred. This is a benchmark-only prototype; it is not
enabled in the application or frozen qualification build. Nothing was staged
or committed, and product source and the frozen input pin remain unchanged.

## Results

| Concurrent requests | Configuration | Median ms | p95 ms | Completed requests/s |
|---:|---|---:|---:|---:|
'''+ '\n'.join(table) + '''

With automatic preparation, pooled median latency was approximately 63% lower
at concurrency 1 and 61% lower at concurrency 4 than the contemporaneous fresh
connection baseline. Worker checkout averaged about 0.05 ms with pooling versus
25.82 ms and 33.02 ms for fresh connections. Admission and worker SQL time also
fell. This does not attribute all improvement exclusively to network handshakes:
session reuse can also avoid session-local initialization and repeated planning.
The experiment does not separately identify those SQL-time effects.

Automatic and immediate preparation produced prepared statements on reused
connections; disabling preparation did not. The inspection itself can add a
prepared statement when immediate preparation is enabled. Variations between
the three pooled configurations were small relative to the pooling effect and
do not establish a consistent benefit from forcing preparation immediately.

The new fresh-connection median was 133.42 ms at concurrency 1, whereas the earlier
separate local check measured 74.61 ms. These are separate sessions with a
different connection-lifetime adapter; the difference was not causally isolated.
Use only the balanced, contemporaneous baseline for this comparison. Do not
present these measurements as a direct 74.61-to-49.56 ms before/after claim.

## Method and scope

Four configurations, two concurrency levels, 100 measured operations per
configuration and 32 excluded warmups per configuration: 800 measured requests
plus 256 warmups. All paths use the same mock payment provider, shared HTTP
client, immutable admission implementation and worker lifecycle implementation.
Requests have unique resources and identities. Configuration order rotates
across ten blocks and reverses on alternating blocks.

SQLAlchemy QueuePool provides four connections per role with zero overflow;
the fresh reference uses NullPool. The admission and worker use separate
authenticated roles and separate pools. The prototype checks out an actual
native psycopg connection, executes the unchanged ExecutionWorker._execute,
and returns the lease with rollback-on-return. Rare lost-acknowledgement
observation/retry connections retain the original fresh, role-bound behavior.
The same lifetime adapter is used for all four configurations. Strict native
connection checks and the claim/fence/outcome commit sequence are preserved.

The fresh reference retains Psycopg's default prepare threshold of five;
pooled variants use disabled, automatic and immediate preparation. No new
dependency was installed. Protocol and script digests were recorded before
measurement. Timings use perf_counter_ns and nearest-rank percentiles.

Application HTTP/authentication, Celery/Redis queue scheduling, startup,
migrations, warmups and cleanup are excluded. This is neither full application
latency nor sustained production capacity. One hundred measurements per
configuration cannot establish a reliable tail-latency guarantee. No performance
SLO or sub-10-ms acceptance threshold was assumed.

## Safety and cleanup

Sixteen actual pool checks passed, covering both preparation-disabled and
immediate-preparation pooled modes:

- Duplicate delivery retains exactly one physical effect.
- Lost claim or fence acknowledgement prevents sending.
- Lost outcome acknowledgement observes committed state without resending.
- A closed/broken connection is discarded and the next operation succeeds.
- Transaction-local state is reset; authenticated database roles are preserved.
- A foreign process is refused before pool checkout.
- Non-idempotent email with a lost response escalates without a second effect.

All 1056 HTTP requests, including warmups, completed with exactly one provider
call and one effect. Every durable operation independently reads back as
SUCCEEDED. Each measured worker observes five SQL calls and three separate
commit acknowledgements. Temporary schemas, roles and mock threads were removed.
All 504 artifacts in eleven earlier sealed packets remain unchanged.

## Before pre-run

The experiment supports implementing bounded connection reuse while retaining
automatic statement preparation. The prototype has not changed runtime behavior.
Enabling it in the application requires a reviewed implementation covering
connection ownership, role isolation, fork lifecycle, rollback/reset and broken
connections, followed by full regressions and an updated source freeze/review.
The existing frozen build still describes the original runtime. Do not start
qualification expecting the experimental pooled timings to apply to it.

Pre-run and big-run execution remain paused. The user must be informed before
pre-run begins; the existing explicit-go requirement for the final big run
remains in force.

Evidence: [protocol](protocol.json), [summary](summary.json),
[all samples](samples.jsonl), [blocks](blocks.json),
[independent provider ledger](provider-ledger.json),
[pool safety checks](pool-safety-checks.json),
[prepared-statement observations](prepared-statements.json),
[source script](experiment.py), [execution log](execution.log),
[cleanup](cleanup.json), [verification](verification.json) and
[artifact hashes](artifact-sha256.json).
''')
assert not scan([OUT]),scan([OUT])
entries=[dict(path=str(path.relative_to(OUT)),sha256=sha(path)) for path in sorted(OUT.rglob('*'))
    if path.is_file() and '__pycache__' not in path.parts and path.name!='artifact-sha256.json']
write('artifact-sha256.json',dict(scope='Local pooling/preparation experiment; no runtime rollout or qualification',files=entries))
for entry in entries:assert sha(OUT/entry['path'])==entry['sha256']
assert not scan([OUT])
print('Verified and sealed',len(entries),'artifacts; 800 measured operations; 16 safety checks; no frozen run.')
