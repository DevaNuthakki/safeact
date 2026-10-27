import hashlib,io,json,shutil,subprocess,tarfile,unittest
from pathlib import Path
from chaos.frozen import ROOT,source_paths,verify
from deploy.evaluation.agent_v1.secret_scan import scan_text
root=ROOT;out=root/'docs/phase-e/dev-baseline-adapter-20261023'
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
manifest,_=verify(root/'chaos/manifest_qual_v1.json')
assert not (root/manifest['budget']['execution_ledger_dir']).exists(),'Frozen execution was started'
base=json.loads((root/'docs/phase-e/dev-baseline-owned-stack-20261023/source-sha256.json').read_text())
paths={entry['path'] for entry in base['files']}|set(source_paths(root))
paths|={str(p.relative_to(root)) for d in ('chaos','tests') for p in (root/d).rglob('*') if p.is_file() and p.suffix in ('.py','.json','.jsonl') and '__pycache__' not in p.parts}
paths|={str(p.relative_to(root)) for p in out.glob('*.py')}
paths|={'README.md','CLAUDE.md','docs/phase-e/observations.md','docs/phase-e/pre-stop2-implementation-plan.md','docs/phase-e/weaker-baseline-implementation-plan.md','docs/phase-e/scenario-construction-audit.md','docs/phase-e/qualification-freeze-plan.md','docs/phase-e/b3-design.md','docs/phase-e/b3-process-adapter-checkpoint.md'}
base_only={entry['path']:entry for entry in base['base_only_files']}
for name,entry in base_only.items():
 assert sha(root/name)==entry['sha256']
 assert (root/name).read_bytes()==subprocess.check_output(['git','show',base['base_commit']+':'+name])
paths-=set(base_only);entries=[]
with tarfile.open(out/'source.tar.gz','w:gz') as archive:
 for name in sorted(paths):
  path=root/name;data=path.read_bytes();assert not scan_text(data.decode(),suffix=path.suffix),name
  info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o644;archive.addfile(info,io.BytesIO(data))
  entries.append(dict(path=name,sha256=hashlib.sha256(data).hexdigest()))
record=dict(scope='Uncommitted pre-STOP-2 source and frozen input snapshot',base_commit=base['base_commit'],uncommitted=True,files=entries,base_only_files=list(base_only.values()))
(out/'source-sha256.json').write_text(json.dumps(record,indent=2)+'\n')
previous={entry['path']:entry['sha256'] for entry in base['files']};current={entry['path']:entry['sha256'] for entry in entries}
delta=dict(added=sorted(set(current)-set(previous)),removed=sorted(set(previous)-set(current)),changed=sorted(name for name in set(current)&set(previous) if current[name]!=previous[name]),unchanged=sum(current[name]==previous[name] for name in set(current)&set(previous)))
(out/'source-delta.json').write_text(json.dumps(delta,indent=2)+'\n')
historical=[]
for relative in ('docs/demo/validation-20261022','docs/phase-e/dev-workflows-20261022','docs/phase-e/dev-recovery-20261022','docs/phase-e/dev-boundaries-20261022','docs/phase-e/dev-baseline-execution-20261023','docs/phase-e/dev-baseline-workflow-review-20261023','docs/phase-e/dev-baseline-observer-20261023','docs/phase-e/dev-baseline-process-trace-20261023','docs/phase-e/dev-baseline-owned-stack-20261023'):
 path=root/relative;metadata=json.loads((path/'artifact-sha256.json').read_text());files=metadata if isinstance(metadata,list) else metadata['files']
 for entry in files:assert sha(path/entry['path'])==entry['sha256'],(relative,entry['path'])
 historical.append(dict(path=relative,artifacts=len(files),artifact_manifest_sha256=sha(path/'artifact-sha256.json')))
assert len(historical)==9 and sum(row['artifacts'] for row in historical)==364
(out/'historical-packets.json').write_text(json.dumps(historical,indent=2)+'\n')

def flatten(suite):
 for test in suite:
  if isinstance(test,unittest.TestSuite):yield from flatten(test)
  else:yield test
suites={}
for key,path in (('phase-a','phase_a'),('integration','integration'),('isolated','isolated'),('api','e2e_api'),('agent','e2e_agent')):
 tests=list(flatten(unittest.defaultTestLoader.discover('tests/'+path,pattern='test_*.py',top_level_dir='.')))
 suites[key]=[test.id() for test in tests]
ids=[name for names in suites.values() for name in names]
assert len(ids)==len(set(ids))==1626,(len(ids),len(set(ids)))
assert {key:len(names) for key,names in suites.items()}=={'phase-a':901,'integration':693,'isolated':21,'api':9,'agent':2}
(out/'test-inventory.json').write_text(json.dumps(dict(count=len(ids),suites=suites),indent=2)+'\n')
(out/'plans').mkdir(exist_ok=True)
for name in ('README.md','CLAUDE.md','docs/phase-e/observations.md','docs/phase-e/pre-stop2-implementation-plan.md','docs/phase-e/weaker-baseline-implementation-plan.md','docs/phase-e/scenario-construction-audit.md','docs/phase-e/qualification-freeze-plan.md'):
 shutil.copyfile(root/name,out/'plans'/Path(name).name)
for name in ('owned-stack-replay','historical-replay','owned-stack-counterexamples','comparison-counterexamples'):
 shutil.copyfile('/tmp/safeact-prestop2-'+name+'.json',out/(name+'.json'))
for name in ('selected-results.jsonl','summary.json'):
 shutil.copyfile('/tmp/safeact-prestop2-selected-replay-corrected/'+name,out/('development-'+name))
print('Prepared',len(entries),'source files and four exact base-only references;',len(ids),'unique Python test IDs;',sum(row['artifacts'] for row in historical),'historical artifacts unchanged')
