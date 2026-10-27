"""Publish local check evidence only; never execute a test or qualification."""
import hashlib
import json
import math
import shutil
import subprocess
from pathlib import Path

from chaos.frozen import verify
from connectors.http_trace import correlation_hash
from deploy.evaluation.agent_v1.secret_scan import HEX64, scan, scan_text

ROOT = Path.cwd()
PRIVATE = Path('/tmp/safeact-local-risk-check-20261024')
OUT = ROOT/'docs/phase-e/local-risk-check-20261024'
OUT.mkdir(exist_ok=False)

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def read(name):
    return json.loads((PRIVATE/name).read_text())

def write(name, value):
    (OUT/name).write_text(json.dumps(value, indent=2)+'\n')

checks = read('test-checks.json')
assert len(checks)==2 and sum(row['tests'] for row in checks)==78
assert all(row['passed'] and row['errors']==row['failures']==row['skipped']==0 for row in checks)
ids = [name for row in checks for name in row['ids']]
assert len(set(ids))==78
rows = [json.loads(line) for line in (PRIVATE/'latency-samples.jsonl').read_text().splitlines()]
blocks = read('latency-blocks.json')
summary = read('latency-summary.json')
assert len(rows)==420 and len({row['resource_id'] for row in rows})==420
assert len(blocks)==40
assert sum(not row['warmup'] for row in rows)==400
assert all(row['provider_calls']==row['observed_calls']==row['observed_effects']==1 for row in rows)
assert summary['errors']==summary['prohibited_duplicate_effects']==0
events = read('provider-ledger.json')
calls = [row for row in events if row['event']=='call']
effects = [row for row in events if row['event']=='effect']
assert len(calls)==len(effects)==420
assert {row['correlation_sha256'] for row in calls}=={row['correlation_sha256'] for row in rows}
assert {row['correlation_sha256'] for row in effects}=={row['correlation_sha256'] for row in rows}
assert len({row['effect_id'] for row in effects})==420
for config in summary['configurations']:
    selected=[row for row in rows if not row['warmup'] and row['path']==config['path'] and row['concurrency']==config['concurrency']]
    assert len(selected)==config['measured_requests']==100
    for field, distribution in config['metrics'].items():
        if field=='commit_by_stage_ms':
            continue
        ordered=sorted(row[field] for row in selected)
        assert distribution['count']==100
        for key, fraction in (('p50',.5),('p95',.95),('p99',.99)):
            assert distribution[key]==ordered[math.ceil(fraction*len(ordered))-1]
        assert math.isclose(distribution['mean'],sum(ordered)/len(ordered),rel_tol=1e-12)
    for row in selected:
        assert math.isclose(row['non_provider_ms']+row['provider_http_ms'],row['total_ms'],rel_tol=1e-12)
        if row['path']=='safeact_durable_worker':
            assert row['committed_state']=='SUCCEEDED'
            assert set(row['commit_ms'])=={'claim','fence','outcome'}
            assert row['worker_sql_execute_count']==5
            assert row['worker_connection_ms']+row['worker_sql_execute_ms']+row['worker_commit_ms']+row['provider_http_ms'] <= row['worker_ms']
assert all(read('cleanup.json').values())
manifest,_=verify(ROOT/'chaos/manifest_qual_v1.json')
assert not (ROOT/manifest['budget']['execution_ledger_dir']).exists()
assert sha(ROOT/'chaos/manifest_qual_v1.json')==read('benchmark-protocol.json')['manifest_sha256']
assert sha(ROOT/'chaos/source_qual_v1.json')==read('benchmark-protocol.json')['source_sha256']

for name in ('benchmark-protocol.json','cleanup.json','latency-blocks.json','latency-samples.jsonl',
             'provider-ledger.json','latency-summary.json','test-checks.json'):
    shutil.copyfile(PRIVATE/name, OUT/name)
log_metadata=[]
for path in sorted(PRIVATE.glob('*.log')):
    original=path.read_bytes()
    text,count=HEX64.subn('[hex64-redacted]',original.decode())
    assert not scan_text(text,suffix='.log')
    (OUT/path.name).write_text(text)
    log_metadata.append(dict(path=path.name, original_sha256=hashlib.sha256(original).hexdigest(),
                             shared_sha256=sha(OUT/path.name), hex64_redactions=count))
write('log-files.json',log_metadata)
for path,name in (
    ('/tmp/safeact-local-risk-tests-20261024.py','targeted-tests.py'),
    ('/tmp/safeact-local-latency-check-20261024.py','latency-check.py'),
    ('/tmp/safeact-package-local-risk-check-20261024.py','package-evidence.py')):
    shutil.copyfile(path,OUT/name)

historical=json.loads((ROOT/'docs/phase-e/dev-baseline-adapter-20261023/historical-packets.json').read_text())
historical.append(dict(path='docs/phase-e/dev-baseline-adapter-20261023', artifacts=123))
unchanged=[]
for row in historical:
    directory=ROOT/row['path']
    metadata=json.loads((directory/'artifact-sha256.json').read_text())
    entries=metadata if isinstance(metadata,list) else metadata['files']
    assert len(entries)==row['artifacts']
    for entry in entries:
        assert sha(directory/entry['path'])==entry['sha256']
    unchanged.append(dict(path=row['path'],artifacts=len(entries),artifact_manifest_sha256=sha(directory/'artifact-sha256.json')))
assert sum(row['artifacts'] for row in unchanged)==487
write('unchanged-packets.json',unchanged)
shutil.copyfile(ROOT/'chaos/source_qual_v1.json',OUT/'source-sha256.json')
assert not subprocess.check_output(['git','diff','--cached','--name-only'])
head=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
assert head=='32693852af7f2deda9adf38c5803df26fec637f3'
subprocess.run(['git','diff','--check'],check=True)
write('verification.json',dict(
    scope='Targeted local recovery tests and bounded worker microbenchmark only',
    targeted_tests_passed=78, skipped=0, measured_requests=400, warmup_requests=20,
    errors=0, duplicate_effects=0, provider_calls=420, provider_effects=420,
    frozen_execution_ledger_absent=True, frozen_inputs_unchanged=True,
    smoke_executed=False, calibration_executed=False, frozen_qualification_executed=False,
    external_provider_calls=0, paid_calls=0, sut_changes=False, commits_created=0,
    index_empty=True, base_commit=head, older_packets_unchanged=10,
    older_packet_artifacts_unchanged=487,
    cleanup=read('cleanup.json'), manifest_sha256=sha(ROOT/'chaos/manifest_qual_v1.json')))

def val(path,concurrency,field,stat):
    return next(row for row in summary['configurations'] if row['path']==path and row['concurrency']==concurrency)['metrics'][field][stat]

table=[]
for concurrency in (1,4):
    for path,label in (('direct_http','Direct mock HTTP'),('safeact_durable_worker','SafeAct admission + worker')):
        config=next(row for row in summary['configurations'] if row['path']==path and row['concurrency']==concurrency)
        m=config['metrics']['total_ms']
        table.append(f'| {concurrency} | {label} | {m["p50"]:.2f} | {m["p95"]:.2f} | {m["p99"]:.2f} | {config["completed_requests_per_second"]:.2f} |')

report='''# Local recovery and latency checks — 2026-10-24

The user requested results for two concerns and explicitly prohibited pre-run
and big-run execution. These checks used existing recovery tests, disposable
PostgreSQL schemas and loopback mock HTTP providers. No frozen scenarios,
smoke sample, calibration, qualification, real Stripe or paid service ran.
Product code and frozen inputs are unchanged. Nothing was staged or committed.

## 1. Provider outcomes that cannot be verified

**The implemented conservative safeguard passes the targeted checks.** All 78
tests passed: 38 policy/contract/metric tests and 40 integration tests, zero skips.
The integration suite includes the non-idempotent email provider applying an
effect then losing its response: SafeAct escalates, a second reconciliation
skips the operation, and the independent provider ledger still has exactly one
call and one effect. Eventual empty lookup does not authorize retry; native-key
expiry and insufficient evidence block unsafe retry. Missing or foreign
contracts/evidence are refused. Lost claim/fence acknowledgement blocks sending;
lost outcome acknowledgement re-observes durable state rather than repeating
the effect. Real socket tests cover a lost reply and a later effect after an
empty lookup.

This is safe handling of uncertainty, not proof that every provider can be
verified or every operation can complete. The provider must supply the relevant
guarantee. Full frozen qualification is still pending.

See [test-checks.json](test-checks.json),
[policy results](policy-and-contracts.log) and
[integration results](recovery-integration.log).

## 2. Latency added by durable execution

**The measured local overhead is material; this concern is not closed.**
Each configuration contains 100 measured requests, plus five excluded warmups.
Direct HTTP and SafeAct use the same loopback payment mock, amount, reusable
HTTP client and unique operation identities. Ten balanced blocks alternate path
order. SafeAct runs real admission with NullPool and the unchanged synchronous
ExecutionWorker. Every admitted operation commits SUCCEEDED, with one physical
provider call and one effect. Setup, migrations, warmup and cleanup are excluded.

| Concurrent requests | Path | Median ms | p95 ms | p99 ms | Completed requests/s |
|---:|---|---:|---:|---:|---:|
'''+ '\n'.join(table) + f'''

At concurrency 1, SafeAct averages {val('safeact_durable_worker',1,'total_ms','mean'):.2f} ms overall:
{val('safeact_durable_worker',1,'admission_ms','mean'):.2f} ms admission,
{val('safeact_durable_worker',1,'worker_connection_ms','mean'):.2f} ms worker connection,
{val('safeact_durable_worker',1,'worker_sql_execute_ms','mean'):.2f} ms worker SQL calls,
{val('safeact_durable_worker',1,'worker_commit_ms','mean'):.2f} ms across the three worker commits,
and {val('safeact_durable_worker',1,'provider_http_ms','mean'):.2f} ms provider HTTP.
The rest is Python/connector processing and timing overhead. Admission already
includes its database work, connections and commit. Thus these figures do not
double-count the worker SQL or its three commits. Five protected SQL calls and
three commit acknowledgements were observed in each successful worker request.
The total measured non-provider overhead averages
{val('safeact_durable_worker',1,'non_provider_ms','mean'):.2f} ms at concurrency 1 and
{val('safeact_durable_worker',4,'non_provider_ms','mean'):.2f} ms at concurrency 4.

Direct HTTP has no durable admission, recovery, fencing or audit guarantees; it
is a lower-bound reference, not an equivalent B0/B1/B2/B3 comparison. These
measurements exclude application HTTP/authentication and Redis/Celery/relay
scheduling, so they are not full application latency or production capacity.
The database is local to the existing development setup and the fixture opens
fresh database connections; do not generalize its cost to a pooled deployment.
At 100 observations per configuration, p99 is a descriptive sample statistic,
not a reliable tail-latency claim. No performance SLO was supplied, so no
latency threshold is labeled passing.

Connection reuse deserves investigation: both admission and the worker establish
fresh connections. The SQL calls also have measurable cost. Any later
optimization should be evaluated against a stated latency target and preserve
the acknowledged dispatch fence before provider transmission. No optimization
or change to those safeguards was attempted here.

See [protocol](benchmark-protocol.json), [summary](latency-summary.json),
[all samples](latency-samples.jsonl), [timed blocks](latency-blocks.json) and the
independent [provider ledger](provider-ledger.json). The scripts
[targeted-tests.py](targeted-tests.py) and [latency-check.py](latency-check.py)
record the exact methods used.

## Scope and evidence checks

All 400 measured requests and 20 warmups completed with zero errors and zero
duplicate effects. Temporary schemas, roles and provider threads were removed.
Frozen input digests still verify and the frozen execution ledger remains
absent. All 487 artifacts in ten earlier sealed packets remain unchanged.
Evidence is scanned for secret-shaped values and sealed by
[artifact-sha256.json](artifact-sha256.json).

The detailed [verification record](verification.json) retains these assertions.
Pre-run and big-run execution remain paused at the user's instruction.
'''
(OUT/'RESULTS.md').write_text(report)
assert not scan([OUT]), scan([OUT])
entries=[dict(path=str(path.relative_to(OUT)),sha256=sha(path)) for path in sorted(OUT.rglob('*')) if path.is_file() and '__pycache__' not in path.parts and path.name!='artifact-sha256.json']
write('artifact-sha256.json',dict(scope='Local risk checks only; no frozen execution',files=entries))
for entry in entries:
    assert sha(OUT/entry['path'])==entry['sha256']
assert not scan([OUT])
print('Verified and sealed',len(entries),'artifacts; 78 tests; 400 measured operations; no frozen execution.')
