import json,time,unittest
from pathlib import Path
from chaos.frozen import ROOT,verify,sha
manifest,_=verify(ROOT/"chaos/manifest_qual_v1.json")
manifest_sha256=sha((ROOT/"chaos/manifest_qual_v1.json").read_bytes())

def flatten(suite):
 for test in suite:
  if isinstance(test,unittest.TestSuite):yield from flatten(test)
  else:yield test
all_tests=list(flatten(unittest.defaultTestLoader.discover('tests/integration',pattern='test_*.py',top_level_dir='.')))
ids=[test.id() for test in all_tests]
assert len(ids)==len(set(ids)), 'Integration discovery includes duplicate test IDs'
last={'tests.integration.test_baseline_arm','tests.integration.test_naive_baseline','tests.integration.test_queue_baselines'}
ordered=[test for test in all_tests if test.__class__.__module__ not in last]+[test for test in all_tests if test.__class__.__module__ in last]
Path('/tmp/safeact-prestop2-integration-corrected-inventory.json').write_text(json.dumps(dict(count=len(ids),ids=ids,deferred_modules=sorted(last),manifest_sha256=manifest_sha256),indent=2)+'\n')
print('Complete integration discovery:',len(ids),'unique tests; common slot-6 adapter tests run after the remaining integration tests.',flush=True)
start=time.monotonic();result=unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite(ordered))
row=dict(check='integration',exit_code=int(not result.wasSuccessful() or bool(result.skipped)),elapsed_seconds=time.monotonic()-start,tests=result.testsRun,skipped=len(result.skipped),log='/tmp/safeact-prestop2-final-integration-corrected.log')
Path('/tmp/safeact-prestop2-integration-corrected-check.json').write_text(json.dumps(row,indent=2)+'\n')
raise SystemExit(row['exit_code'])
