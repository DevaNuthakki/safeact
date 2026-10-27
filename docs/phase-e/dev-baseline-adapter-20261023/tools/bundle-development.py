import hashlib,io,json,tarfile
from pathlib import Path
from deploy.evaluation.agent_v1.secret_scan import scan_text
out=Path('docs/phase-e/dev-baseline-adapter-20261023')
runs={
'b3-first':'/tmp/safeact-prestop2-b3-development-first',
'weaker-first':'/tmp/safeact-prestop2-weaker-development-first',
'weaker-marker-corrected':'/tmp/safeact-prestop2-weaker-development-corrected',
'b1-lifecycle-corrected':'/tmp/safeact-prestop2-b1-development-lifecycle-corrected',
'safeact-first':'/tmp/safeact-prestop2-safeact-development-first',
'b3-audit':'/tmp/safeact-prestop2-b3-development-audit',
'breaker':'/tmp/safeact-prestop2-breaker-development-first',
'workflow-accounting':'/tmp/safeact-prestop2-workflow-accounting-development',
}
entries=[]
with tarfile.open(out/'development-runs.tar.gz','w:gz') as archive:
 for label,directory in runs.items():
  for path in sorted(Path(directory).glob('*')):
   if path.suffix not in ('.json','.jsonl'):continue
   data=path.read_bytes();assert not scan_text(data.decode(),suffix=path.suffix),(label,path.name)
   name=label+'/'+path.name;info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o644
   archive.addfile(info,io.BytesIO(data));entries.append(dict(path=name,sha256=hashlib.sha256(data).hexdigest(),bytes=len(data)))
(out/'development-run-files.json').write_text(json.dumps(dict(scope='Development and retained diagnostic originals only; no frozen outcomes',files=entries),indent=2)+'\n')
focus=[]
with tarfile.open(out/'focused-captures.tar.gz','w:gz') as archive:
 for directory in sorted(Path('/tmp').glob('safeact-prestop2-*capture')):
  for path in sorted(directory.glob('*.json')):
   data=path.read_bytes();assert not scan_text(data.decode(),suffix='.json'),str(path)
   name=directory.name+'/'+path.name;info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o644
   archive.addfile(info,io.BytesIO(data));focus.append(dict(path=name,sha256=hashlib.sha256(data).hexdigest(),bytes=len(data)))
(out/'focused-capture-files.json').write_text(json.dumps(dict(files=focus),indent=2)+'\n')
print('Bundled',len(entries),'development files and',len(focus),'focused captures; originals unchanged')
