"""Verify saved evidence and current source. Never execute scenarios or providers."""
import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

from chaos.frozen import verify
from deploy.evaluation.agent_v1.secret_scan import scan, scan_text

ROOT = Path(__file__).resolve().parents[4]
PACKET = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(name):
    return json.loads((PACKET/name).read_text())


def entries(value):
    return value if isinstance(value, list) else value['files']


def archive(name, metadata, *, current=False):
    expected = {entry['path']: entry['sha256'] for entry in entries(read(metadata))}
    with tarfile.open(PACKET/name) as saved:
        members = saved.getmembers()
        assert len(members) == len(expected)
        assert {member.name for member in members} == set(expected)
        for member in members:
            assert member.isfile() and not member.name.startswith('/')
            assert '..' not in Path(member.name).parts
            data = saved.extractfile(member).read()
            assert digest(data) == expected[member.name], member.name
            assert not scan_text(data.decode(), suffix=Path(member.name).suffix), member.name
            if current:
                assert (ROOT/member.name).read_bytes() == data, member.name
    return len(expected)


def main():
    manifest, cases = verify(ROOT/'chaos/manifest_qual_v1.json')
    assert len(cases) == 1200 and len(manifest['smoke_ids']) == 80
    assert not (ROOT/manifest['budget']['execution_ledger_dir']).exists()
    sealed = entries(read('artifact-sha256.json'))
    assert len(sealed) == len({entry['path'] for entry in sealed})
    actual = {str(path.relative_to(PACKET)) for path in PACKET.rglob('*')
              if path.is_file() and '__pycache__' not in path.parts
              and path != PACKET/'artifact-sha256.json'}
    assert actual == {entry['path'] for entry in sealed}
    for entry in sealed:
        assert digest((PACKET/entry['path']).read_bytes()) == entry['sha256'], entry['path']
    assert not scan([PACKET])
    assert archive('source.tar.gz', 'source-sha256.json', current=True) == 470
    assert archive('development-runs.tar.gz', 'development-run-files.json') == 29
    assert archive('focused-captures.tar.gz', 'focused-capture-files.json') == 25
    source = read('source-sha256.json')
    assert len(source['base_only_files']) == 4
    for entry in source['base_only_files']:
        data = (ROOT/entry['path']).read_bytes()
        assert digest(data) == entry['sha256']
        assert data == subprocess.check_output(
            ['git', 'show', source['base_commit']+':'+entry['path']], cwd=ROOT)
    history = read('historical-packets.json')
    assert len(history) == 9 and sum(row['artifacts'] for row in history) == 364
    for row in history:
        base = ROOT/row['path']
        assert digest((base/'artifact-sha256.json').read_bytes()) == row['artifact_manifest_sha256']
        saved = entries(json.loads((base/'artifact-sha256.json').read_text()))
        assert len(saved) == row['artifacts']
        for entry in saved:
            assert digest((base/entry['path']).read_bytes()) == entry['sha256']
    inventory = read('test-inventory.json')
    ids = [name for names in inventory['suites'].values() for name in names]
    assert len(ids) == len(set(ids)) == inventory['count'] == 1626
    proof = read('integration-source-proof.json')
    assert digest((ROOT/'chaos/manifest_qual_v1.json').read_bytes()) == proof['current_manifest_sha256']
    assert digest((PACKET/'integration-start-manifest.json').read_bytes()) == proof['integration_manifest_sha256']
    start = read('integration-start-manifest.json')
    assert digest((PACKET/'integration-start-source-sha256.json').read_bytes()) == start['inputs']['source']['sha256']
    old = {entry['path']: entry['sha256'] for entry in read('integration-start-source-sha256.json')['files']}
    current = {entry['path']: entry['sha256'] for entry in json.loads((ROOT/'chaos/source_qual_v1.json').read_text())['files']}
    assert set(old) == set(current)
    assert sorted(name for name in old if old[name] != current[name]) == proof['changed_files'] == [
        '.github/workflows/ci.yml', '.github/workflows/qualification.yml']
    assert proof['python_sut_changes'] == []
    assert read('comparison-counterexamples.json')['rejected'] == 25
    assert read('owned-stack-counterexamples.json')['rejected'] == 33
    checks = read('regression-checks.json')
    assert checks['python_tests'] == 1626 and checks['console_tests'] == 51
    assert checks['integration_skips'] == 0 and all(row['passed'] for row in checks['checks'])
    assert not subprocess.check_output(['git', 'diff', '--cached', '--name-only'], cwd=ROOT)
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip() == source['base_commit']
    subprocess.run(['git', 'diff', '--check'], cwd=ROOT, check=True)
    print('Verified', len(sealed), 'packet artifacts; 470 source files + four base references;')
    print('364 historical artifacts unchanged; 1200 cases / 80 sample pinned; no frozen invocation.')


if __name__ == '__main__':
    main()
