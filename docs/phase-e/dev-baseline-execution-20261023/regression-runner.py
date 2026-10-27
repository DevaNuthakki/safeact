import os,subprocess,sys,time
from pathlib import Path
root=Path('/Users/deva/Documents/personal-projects/safeact'); py=str(root/'.venv/bin/python')
checks=[
('isolated',['SAFEACT_RUN_ISOLATED_CLUSTER_TESTS'],[py,'-m','unittest','discover','-s','tests/isolated','-t','.','-p','test_*.py'],root),
('api',['SAFEACT_RUN_API_E2E'],[py,'-m','unittest','discover','-s','tests/e2e_api','-t','.','-p','test_*.py'],root),
('agent',['SAFEACT_RUN_AGENT_HARNESS'],[py,'-m','unittest','discover','-s','tests/e2e_agent','-t','.','-p','test_*.py'],root),
('browser',[],['npx','--no-install','playwright','test'],root/'ui'),
('integration',['SAFEACT_RUN_DB_INTEGRATION','SAFEACT_RUN_DISPOSABLE_DB_TESTS','SAFEACT_RUN_DB_SECURITY_TESTS','SAFEACT_RUN_BROKER_TESTS','SAFEACT_RUN_ISOLATED_REDIS_TESTS'],[py,'-m','unittest','discover','-s','tests/integration','-t','.','-p','test_*.py'],root),
]
for name,flags,command,cwd in checks:
    path=Path('/tmp')/f'safeact-baseline-execution-{name}.log'; started=time.monotonic()
    with path.open('x') as log:
        result=subprocess.run(command,cwd=cwd,env={**os.environ,**{f:'1' for f in flags}},stdout=log,stderr=subprocess.STDOUT)
    print(name,'exit='+str(result.returncode),f'seconds={time.monotonic()-started:.1f}',flush=True)
    if result.returncode:sys.exit(result.returncode)
