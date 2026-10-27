"""Full ordinary integration discovery; never a qualification invocation."""
import json
import os
import time
import unittest
from pathlib import Path

for key in ('SAFEACT_RUN_DB_INTEGRATION','SAFEACT_RUN_DISPOSABLE_DB_TESTS',
            'SAFEACT_RUN_DB_SECURITY_TESTS','SAFEACT_RUN_BROKER_TESTS',
            'SAFEACT_RUN_ISOLATED_REDIS_TESTS'):
    os.environ[key]='1'
from chaos.frozen import ROOT,verify,sha
out=Path('/tmp/safeact-pool-integration-20261024')
manifest,_=verify(ROOT/'chaos/manifest_qual_v1.json')
assert not (ROOT/manifest['budget']['execution_ledger_dir']).exists()
def flatten(suite):
    for test in suite:
        if isinstance(test,unittest.TestSuite):yield from flatten(test)
        else:yield test
tests=list(flatten(unittest.defaultTestLoader.discover('tests/integration',pattern='test_*.py',top_level_dir='.')))
ids=[test.id() for test in tests]
assert len(ids)==len(set(ids))
last={'tests.integration.test_baseline_arm','tests.integration.test_naive_baseline','tests.integration.test_queue_baselines'}
ordered=[test for test in tests if test.__class__.__module__ not in last]+[test for test in tests if test.__class__.__module__ in last]
(out/'integration-inventory.json').write_text(json.dumps(dict(count=len(ids),ids=ids,manifest_sha256=sha((ROOT/'chaos/manifest_qual_v1.json').read_bytes())),indent=2)+'\n')
print('Complete ordinary integration discovery:',len(ids),'unique tests; frozen execution prohibited.',flush=True)
started=time.monotonic()
result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(ordered))
record=dict(check='integration',tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),
    skipped=len(result.skipped),passed=result.wasSuccessful() and not result.skipped,
    elapsed_seconds=time.monotonic()-started,log='final-integration.log')
(out/'integration-check.json').write_text(json.dumps(record,indent=2)+'\n')
assert not (ROOT/manifest['budget']['execution_ledger_dir']).exists()
raise SystemExit(0 if record['passed'] else 1)
