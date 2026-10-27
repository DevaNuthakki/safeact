"""Read-only evidence verification; never start services, tests or qualification."""
import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

from chaos.frozen import verify
from deploy.evaluation.agent_v1.secret_scan import scan,scan_text

ROOT=Path(__file__).resolve().parents[4]
PACKET=Path(__file__).resolve().parents[1]
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read(name):return json.loads((PACKET/name).read_text())
def entries(value):return value if isinstance(value,list) else value['files']

def main():
    manifest,cases=verify(ROOT/'chaos/manifest_qual_v1.json')
    assert len(cases)==1200 and len(manifest['smoke_ids'])==80
    assert not (ROOT/manifest['budget']['execution_ledger_dir']).exists()
    saved=entries(read('artifact-sha256.json'))
    actual={str(path.relative_to(PACKET)) for path in PACKET.rglob('*')
        if path.is_file() and '__pycache__' not in path.parts and path!=PACKET/'artifact-sha256.json'}
    assert actual=={row['path'] for row in saved}
    assert len(saved)==len(actual)
    for row in saved:assert sha(PACKET/row['path'])==row['sha256'],row['path']
    assert not scan([PACKET])
    source=read('source-sha256.json')
    expected={row['path']:row['sha256'] for row in source['files']}
    with tarfile.open(PACKET/'source.tar.gz') as archive:
        members=archive.getmembers()
        assert len(members)==len(expected)
        assert {member.name for member in members}==set(expected)
        for member in members:
            assert member.isfile() and not member.name.startswith('/') and '..' not in Path(member.name).parts
            data=archive.extractfile(member).read()
            assert hashlib.sha256(data).hexdigest()==expected[member.name]
            assert data==(ROOT/member.name).read_bytes()
            assert not scan_text(data.decode(),suffix=Path(member.name).suffix)
    assert len(source['base_only_files'])==4
    for row in source['base_only_files']:
        data=(ROOT/row['path']).read_bytes()
        assert hashlib.sha256(data).hexdigest()==row['sha256']
        assert data==subprocess.check_output(['git','show',source['base_commit']+':'+row['path']],cwd=ROOT)
    history=read('historical-packets.json')
    assert len(history)==12 and sum(row['artifacts'] for row in history)==519
    for row in history:
        directory=ROOT/row['path']
        assert sha(directory/'artifact-sha256.json')==row['artifact_manifest_sha256']
        files=entries(json.loads((directory/'artifact-sha256.json').read_text()))
        assert len(files)==row['artifacts']
        for entry in files:assert sha(directory/entry['path'])==entry['sha256']
    inventory=read('test-inventory.json')
    ids=[name for names in inventory['suites'].values() for name in names]
    assert len(ids)==len(set(ids))==inventory['count']==1650
    assert read('integration-check.json')['passed'] and read('integration-check.json')['skipped']==0
    assert all(row['exit_code']==0 for row in read('surface-checks.json'))
    old=read('previous-inputs/manifest_qual_v1.json')
    for key in old['inputs']:
        if key!='source':assert old['inputs'][key]==manifest['inputs'][key]
    for key in old:
        if key!='inputs':assert old[key]==manifest[key]
    assert read('integration-inventory.json')['manifest_sha256']==sha(ROOT/'chaos/manifest_qual_v1.json')
    assert all(read('latency/cleanup.json').values())
    assert read('latency/summary.json')['errors']==read('latency/summary.json')['duplicate_effects']==0
    assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()==source['base_commit']
    assert not subprocess.check_output(['git','diff','--cached','--name-only'],cwd=ROOT)
    subprocess.run(['git','diff','--check'],cwd=ROOT,check=True)
    print('Verified',len(saved),'packet artifacts;',len(expected),'source files + four exact base references;')
    print('1650 Python / 51 console regressions; 519 historical artifacts unchanged; no pre-run invocation.')

if __name__=='__main__':main()
