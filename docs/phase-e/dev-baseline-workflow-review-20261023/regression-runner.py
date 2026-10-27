import os,subprocess,sys,time
from pathlib import Path
root=Path.cwd();py=str(root/'.venv/bin/python')
checks=[
 ('development',['SAFEACT_RUN_BASELINE_DEV_CHECK','SAFEACT_RUN_DB_INTEGRATION','SAFEACT_RUN_DISPOSABLE_DB_TESTS'],
  [py,'docs/phase-e/dev-baseline-workflow-review-20261023/devcheck.py','--out','/tmp/safeact-b3-workflow-review-development'],root),
 ('phase-a',[],[py,'-m','unittest','discover','-s','tests/phase_a','-t','.','-p','test_*.py'],root),
 ('isolated',['SAFEACT_RUN_ISOLATED_CLUSTER_TESTS'],[py,'-m','unittest','discover','-s','tests/isolated','-t','.','-p','test_*.py'],root),
 ('api',['SAFEACT_RUN_API_E2E'],[py,'-m','unittest','discover','-s','tests/e2e_api','-t','.','-p','test_*.py'],root),
 ('agent',['SAFEACT_RUN_AGENT_HARNESS'],[py,'-m','unittest','discover','-s','tests/e2e_agent','-t','.','-p','test_*.py'],root),
 ('browser',[],['npx','--no-install','playwright','test'],root/'ui'),
 ('integration',['SAFEACT_RUN_DB_INTEGRATION','SAFEACT_RUN_DISPOSABLE_DB_TESTS','SAFEACT_RUN_DB_SECURITY_TESTS','SAFEACT_RUN_BROKER_TESTS','SAFEACT_RUN_ISOLATED_REDIS_TESTS'],
  [py,'-m','unittest','discover','-s','tests/integration','-t','.','-p','test_*.py'],root),
 ('console-unit',[],['npm','test'],root/'ui'),('typecheck',[],['npm','run','typecheck'],root/'ui'),
 ('lint',[],['npm','run','lint'],root/'ui'),('build',[],['npm','run','build'],root/'ui')]
results=[]
for name,flags,command,cwd in checks:
 path=Path('/tmp')/f'safeact-b3-workflow-review-{name}.log';start=time.monotonic()
 with path.open('x') as log:
  result=subprocess.run(command,cwd=cwd,env={**os.environ,'PYTHONPATH':str(root),**{f:'1' for f in flags}},stdout=log,stderr=subprocess.STDOUT)
 row=dict(check=name,exit_code=result.returncode,elapsed_seconds=time.monotonic()-start,command=command,cwd=str(cwd),enabled_flags=flags,log=str(path))
 results.append(row);Path('/tmp/safeact-b3-workflow-review-checks.json').write_text(__import__('json').dumps(results,indent=2)+'\n')
 print(name,'exit='+str(result.returncode),f'seconds={row["elapsed_seconds"]:.1f}',flush=True)
 if result.returncode:sys.exit(result.returncode)
