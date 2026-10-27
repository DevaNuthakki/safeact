"""Package already-completed local checks. Does not execute qualification."""
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from deploy.evaluation.agent_v1.secret_scan import (
    AGENT_KEY, HEX64, OPENAI_KEY, STRIPE_KEY, scan, scan_text,
)

root = Path.cwd()
out = root/'docs/phase-e/dev-baseline-adapter-20261023'
prefix = 'safeact-prestop2-'

def sha(data):
    return hashlib.sha256(data).hexdigest()

def write(name, value):
    (out/name).write_text(json.dumps(value, indent=2)+'\n')

(out/'regressions').mkdir(exist_ok=True)
(out/'tools').mkdir(exist_ok=True)
logs = []
for path in sorted(Path('/tmp').glob(prefix+'*.log')):
    original = path.read_bytes()
    content = original.decode('utf-8', errors='replace')
    counts = {}
    for kind, pattern in (('hex64', HEX64), ('openai_key', OPENAI_KEY),
                          ('agent_key', AGENT_KEY), ('stripe_key', STRIPE_KEY)):
        content, counts[kind] = pattern.subn('['+kind+'-redacted]', content)
    target = Path('regressions')/path.name.removeprefix(prefix)
    assert not scan_text(content, suffix='.log'), str(target)
    (out/target).write_text(content)
    logs.append(dict(path=str(target), original_sha256=sha(original),
        shared_sha256=sha((out/target).read_bytes()), redactions=counts))
write('regression-log-files.json', dict(
    scope='Retained diagnostics and passing reruns; original digests precede shared-log redaction', files=logs))

for name in ('surface-checks', 'integration-corrected-check',
             'integration-corrected-inventory', 'corrected-integration-checks'):
    shutil.copyfile('/tmp/'+prefix+name+'.json', out/'regressions'/(name+'.json'))
for name in ('prepare-packet', 'bundle-development', 'ci-source-proof',
             'surface-validation', 'corrected-integration',
             'full-integration-corrected-runner', 'workflow-accounting', 'seal-packet'):
    shutil.copyfile('/tmp/'+prefix+name+'.py', out/'tools'/(name+'.py'))

inventory = json.loads((out/'test-inventory.json').read_text())
integration = json.loads((out/'regressions/integration-corrected-check.json').read_text())
actual_ids = json.loads((out/'regressions/integration-corrected-inventory.json').read_text())
assert set(actual_ids['ids']) == set(inventory['suites']['integration'])
assert integration['exit_code'] == integration['skipped'] == 0
assert integration['tests'] == 693
surface = json.loads((out/'regressions/surface-checks.json').read_text())
assert len(surface) == 8 and all(check['exit_code'] == 0 for check in surface)
checks = []
for suite, tests, log in (
    ('phase-a', 901, 'final-phase-a-ci-amendment.log'),
    ('integration', 693, 'final-integration-corrected.log'),
    ('isolated', 21, 'final-isolated.log'),
    ('api', 9, 'final-api.log'), ('agent', 2, 'final-agent.log')):
    text = (out/'regressions'/log).read_text()
    assert re.search(r'^Ran '+str(tests)+r' tests in [0-9.]+s\s+OK\s*$', text, re.MULTILINE), suite
    assert len(inventory['suites'][suite]) == tests
    checks.append(dict(check=suite, tests=tests, passed=True, log='regressions/'+log))
assert inventory['count'] == sum(check['tests'] for check in checks) == 1626
for check in surface:
    row = dict(check)
    row['log'] = 'regressions/'+Path(row['log']).name.removeprefix(prefix)
    row['passed'] = True
    if row['check'] in ('isolated', 'api', 'agent'):
        continue
    if row['check'] == 'browser':
        row['tests'] = 14
        assert re.search(r'\b14 passed\b', (out/row['log']).read_text())
    if row['check'] == 'console-unit':
        row['tests'] = 37
        assert re.search(r'Tests\s+37 passed', (out/row['log']).read_text())
    checks.append(row)
assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() == '32693852af7f2deda9adf38c5803df26fec637f3'
assert not subprocess.check_output(['git', 'diff', '--cached', '--name-only'])
subprocess.run(['git', 'diff', '--check'], check=True)
write('regression-checks.json', dict(
    scope='STOP 2 completed local regression; diagnostic originals retained separately',
    python_tests=1626, console_tests=51, integration_skips=0,
    checks=checks, integration_elapsed_seconds=integration['elapsed_seconds'],
    hosted_ci_executed=False, workflow_yaml_parse_passed=True,
    workflow_yaml_log='regressions/workflow-yaml.log',
    source_proof='integration-source-proof.json',
    original_diagnostics='regression-log-files.json',
    index_empty=True, base_commit='32693852af7f2deda9adf38c5803df26fec637f3',
    frozen_execution_started=False, real_stripe_calls=0, paid_model_calls=0,
    commits_created=0, pushes=0))
findings = scan([out])
assert not findings, {name: rows[:3] for name, rows in findings.items()}
entries = [dict(path=str(path.relative_to(out)), sha256=sha(path.read_bytes()))
           for path in sorted(out.rglob('*'))
           if path.is_file() and '__pycache__' not in path.parts
           and path != out/'artifact-sha256.json']
write('artifact-sha256.json', dict(scope='STOP 2 pre-run implementation, source and development evidence only', files=entries))
assert not scan([out])
print('Sealed', len(entries), 'artifacts;', len(logs), 'retained logs;',
      '1626 Python / 51 console tests; scanner and diff checks pass')
