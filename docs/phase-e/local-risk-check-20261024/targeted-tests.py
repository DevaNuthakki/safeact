"""Targeted recovery checks only; no frozen runner, smoke, or calibration."""
import contextlib
import hashlib
import json
import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path.cwd()
OUT = Path('/tmp/safeact-local-risk-check-20261024')
OUT.mkdir(mode=0o700, exist_ok=True)
os.environ['SAFEACT_RUN_DB_INTEGRATION'] = '1'
os.environ['SAFEACT_RUN_DISPOSABLE_DB_TESTS'] = '1'

def local_network_only(event, args):
    if event == 'socket.connect':
        address = args[1]
        if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1'):
            raise RuntimeError('Non-loopback connection forbidden in local risk checks')

sys.addaudithook(local_network_only)

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def flatten(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from flatten(test)
        else:
            yield test

groups = {
    'policy-and-contracts': [
        'tests.phase_a.test_recovery_policy',
        'tests.phase_a.test_contract_hardening',
        'tests.phase_a.test_connector_profiles',
        'tests.phase_a.test_chaos_metrics',
    ],
    'recovery-integration': [
        'tests.integration.test_connector_profile_faults',
        'tests.integration.test_durable_execution_crash_recovery',
        'tests.integration.test_provider_trace_socket',
        'tests.integration.test_connector_contracts',
    ],
}
checks = []
for name, modules in groups.items():
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    ids = [test.id() for test in flatten(suite)]
    assert len(ids) == len(set(ids))
    started = time.monotonic()
    with (OUT/(name+'.log')).open('w') as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    row = dict(check=name, modules=modules, ids=ids, tests=result.testsRun,
        errors=len(result.errors), failures=len(result.failures), skipped=len(result.skipped),
        passed=result.wasSuccessful() and not result.skipped,
        elapsed_seconds=time.monotonic()-started, log=name+'.log')
    checks.append(row)
    (OUT/'test-checks.json').write_text(json.dumps(checks, indent=2)+'\n')
    print(name, row['tests'], 'tests; passed='+str(row['passed']), flush=True)
    if not row['passed']:
        sys.exit(1)
