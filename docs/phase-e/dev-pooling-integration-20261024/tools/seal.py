"""Seal completed integration evidence. No service or qualification execution."""
import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
import unittest
from pathlib import Path

from chaos.frozen import ROOT,verify,source_paths
from deploy.evaluation.agent_v1.secret_scan import HEX64,scan,scan_text

PRIVATE=Path('/tmp/safeact-pool-integration-20261024')
OUT=ROOT/'docs/phase-e/dev-pooling-integration-20261024'
OUT.mkdir(exist_ok=True)
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read(path):return json.loads(Path(path).read_text())
def write(name,value):(OUT/name).write_text(json.dumps(value,indent=2)+'\n')
def entries(value):return value if isinstance(value,list) else value['files']

manifest,cases=verify(ROOT/'chaos/manifest_qual_v1.json')
assert len(cases)==1200 and len(manifest['smoke_ids'])==80
assert not (ROOT/manifest['budget']['execution_ledger_dir']).exists()
old=read(PRIVATE/'before-manifest_qual_v1.json')
for key in old['inputs']:
    if key!='source':assert old['inputs'][key]==manifest['inputs'][key]
for key in old:
    if key!='inputs':assert old[key]==manifest[key]
assert sha(ROOT/'chaos/manifest_qual_v1.json')==read(PRIVATE/'integration-inventory.json')['manifest_sha256']
integration=read(PRIVATE/'integration-check.json')
assert integration['passed'] and integration['failures']==integration['errors']==integration['skipped']==0
surface=read(PRIVATE/'surface-checks.json')
assert len(surface)==8 and all(row['exit_code']==0 for row in surface)
assert all(read(PRIVATE/'latency/cleanup.json').values())
summary=read(PRIVATE/'latency/summary.json')
assert summary['measured_operations']==400 and summary['warmups']==128
assert summary['errors']==summary['duplicate_effects']==0
samples=[json.loads(line) for line in (PRIVATE/'latency/samples.jsonl').read_text().splitlines()]
assert len(samples)==528 and len({row['resource_id'] for row in samples})==528
assert sum(not row['warmup'] for row in samples)==400
assert all(row['committed_state']=='SUCCEEDED' and row['provider_calls']==row['observed_calls']==row['observed_effects']==1 for row in samples)
events=read(PRIVATE/'latency/provider-ledger.json')
calls=[row for row in events if row['event']=='call'];effects=[row for row in events if row['event']=='effect']
assert len(calls)==len(effects)==528
assert {row['correlation_sha256'] for row in calls}=={row['correlation_sha256'] for row in samples}
assert {row['correlation_sha256'] for row in effects}=={row['correlation_sha256'] for row in samples}
for config in summary['configurations']:
    selected=[row for row in samples if not row['warmup'] and row['mode']==config['mode'] and row['concurrency']==config['concurrency']]
    assert len(selected)==100
    for key,record in config['metrics'].items():
        values=sorted(row[key] for row in selected)
        for label,fraction in (('p50',.5),('p95',.95),('p99',.99)):
            assert record[label]==values[__import__('math').ceil(fraction*len(values))-1]

def flatten(suite):
    for test in suite:
        if isinstance(test,unittest.TestSuite):yield from flatten(test)
        else:yield test
suites={}
for name,folder in (('phase-a','phase_a'),('integration','integration'),('isolated','isolated'),('api','e2e_api'),('agent','e2e_agent')):
    suites[name]=[test.id() for test in flatten(unittest.defaultTestLoader.discover('tests/'+folder,pattern='test_*.py',top_level_dir='.'))]
ids=[name for names in suites.values() for name in names]
assert len(ids)==len(set(ids))==1650
assert {key:len(names) for key,names in suites.items()}==dict(zip(('phase-a','integration','isolated','api','agent'),(908,710,21,9,2)))
assert set(read(PRIVATE/'integration-inventory.json')['ids'])==set(suites['integration'])
for suite,filename in (('phase-a','final-phase-a-socket-enabled.log'),('integration','final-integration.log'),
                       ('isolated','final-isolated.log'),('api','final-api.log'),('agent','final-agent.log')):
    text=(PRIVATE/filename).read_text()
    assert re.search(r'^Ran '+str(len(suites[suite]))+r' tests in [0-9.]+s\s+OK\s*$',text,re.MULTILINE),suite
assert re.search(r'\b14 passed\b',(PRIVATE/'final-browser.log').read_text())
assert re.search(r'Tests\s+37 passed',(PRIVATE/'final-console-unit.log').read_text())
write('test-inventory.json',dict(count=len(ids),suites=suites))

(OUT/'regressions').mkdir(exist_ok=True)
logs=[]
for path in sorted(PRIVATE.glob('*.log')):
    original=path.read_bytes();content,count=HEX64.subn('[hex64-redacted]',original.decode(errors='replace'))
    assert not scan_text(content,suffix='.log'),path.name
    (OUT/'regressions'/path.name).write_text(content)
    logs.append(dict(path='regressions/'+path.name,original_sha256=hashlib.sha256(original).hexdigest(),
        shared_sha256=sha(OUT/'regressions'/path.name),hex64_redactions=count))
write('regression-log-files.json',logs)
for name in ('baseline.json','source-amendment.json','integration-check.json','integration-inventory.json','surface-checks.json'):
    shutil.copyfile(PRIVATE/name,OUT/name)
for path in PRIVATE.glob('first-*.json'):
    shutil.copyfile(path,OUT/path.name)
shutil.copyfile(PRIVATE/'shutdown-race-diagnostic.json',OUT/'shutdown-race-diagnostic.json')
shutil.copytree(PRIVATE/'first-validation-source',OUT/'first-validation-source',dirs_exist_ok=True)
(OUT/'previous-inputs').mkdir(exist_ok=True)
for path in PRIVATE.glob('before-*'):shutil.copyfile(path,OUT/'previous-inputs'/path.name.removeprefix('before-'))
(OUT/'latency').mkdir(exist_ok=True)
for path in (PRIVATE/'latency').glob('*'):
    if path.is_file():shutil.copyfile(path,OUT/'latency'/path.name)
(OUT/'tools').mkdir(exist_ok=True)
for path,name in (('/tmp/safeact-pool-full-integration-20261024.py','full-integration.py'),
                  ('/tmp/safeact-pool-surface-validation-20261024.py','surface-validation.py'),
                  ('/tmp/safeact-pool-integrated-latency-20261024.py','integrated-latency.py'),
                  (__file__,'seal.py')):
    shutil.copyfile(path,OUT/'tools'/name)

base=read(ROOT/'docs/phase-e/dev-baseline-adapter-20261023/source-sha256.json')
paths={row['path'] for row in base['files']}|set(source_paths(ROOT))
paths|={str(path.relative_to(ROOT)) for folder in ('chaos','tests') for path in (ROOT/folder).rglob('*')
    if path.is_file() and path.suffix in ('.py','.json','.jsonl') and '__pycache__' not in path.parts}
paths.add('docs/phase-e/pooling-integration-plan.md')
paths-={row['path'] for row in base['base_only_files']}
source=[]
with tarfile.open(OUT/'source.tar.gz','w:gz') as archive:
    for name in sorted(paths):
        path=ROOT/name;data=path.read_bytes()
        assert not scan_text(data.decode(),suffix=path.suffix),name
        info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o644
        archive.addfile(info,io.BytesIO(data));source.append(dict(path=name,sha256=hashlib.sha256(data).hexdigest()))
for row in base['base_only_files']:
    assert sha(ROOT/row['path'])==row['sha256']
    assert (ROOT/row['path']).read_bytes()==subprocess.check_output(['git','show',base['base_commit']+':'+row['path']])
write('source-sha256.json',dict(base_commit=base['base_commit'],uncommitted=True,files=source,base_only_files=base['base_only_files']))
old_source={row['path']:row['sha256'] for row in base['files']};new_source={row['path']:row['sha256'] for row in source}
write('source-delta.json',dict(added=sorted(set(new_source)-set(old_source)),
    changed=sorted(name for name in old_source if old_source[name]!=new_source.get(name)),
    removed=sorted(set(old_source)-set(new_source))))

historical=read(ROOT/'docs/phase-e/local-pooling-experiment-20261024/unchanged-packets.json')
historical.append(dict(path='docs/phase-e/local-pooling-experiment-20261024',artifacts=15))
unchanged=[]
for row in historical:
    directory=ROOT/row['path'];saved=entries(read(directory/'artifact-sha256.json'))
    assert len(saved)==row['artifacts']
    for entry in saved:assert sha(directory/entry['path'])==entry['sha256']
    unchanged.append(dict(path=row['path'],artifacts=len(saved),artifact_manifest_sha256=sha(directory/'artifact-sha256.json')))
assert len(unchanged)==12 and sum(row['artifacts'] for row in unchanged)==519
write('historical-packets.json',unchanged)
(OUT/'plans').mkdir(exist_ok=True)
for name in ('CLAUDE.md','README.md','docs/phase-e/observations.md','docs/phase-e/pooling-integration-plan.md'):
    shutil.copyfile(ROOT/name,OUT/'plans'/Path(name).name)
subprocess.run(['git','diff','--check'],check=True)
assert not subprocess.check_output(['git','diff','--cached','--name-only'])
assert subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()==base['base_commit']
write('verification.json',dict(scope='Runtime pooling integration; ordinary regressions and local timing only',
    python_tests=1650,console_tests=51,integration_skips=0,
    source_files=len(source),base_only_files=4,backend_source_files=len(read(ROOT/'chaos/source_qual_v1.json')['files']),
    historical_packets=12,historical_artifacts=519,measured_operations=400,warmups=128,
    errors=0,duplicate_effects=0,pre_run_executed=False,smoke_executed=False,calibration_executed=False,
    big_run_executed=False,frozen_execution_ledger_absent=True,external_provider_calls=0,paid_model_calls=0,
    commits_created=0,index_empty=True,manifest_sha256=sha(ROOT/'chaos/manifest_qual_v1.json')))
assert (OUT/'REVIEW.md').exists(),'Write the final review before sealing'
assert not scan([OUT]),scan([OUT])
artifacts=[dict(path=str(path.relative_to(OUT)),sha256=sha(path)) for path in sorted(OUT.rglob('*'))
    if path.is_file() and '__pycache__' not in path.parts and path.name!='artifact-sha256.json']
write('artifact-sha256.json',dict(scope='Completed runtime integration; pre-run explicitly paused',files=artifacts))
for entry in artifacts:assert sha(OUT/entry['path'])==entry['sha256']
assert not scan([OUT])
print('Sealed',len(artifacts),'artifacts;',len(source),'source files; 1650 Python / 51 console tests; no pre-run.')
