import json,os,subprocess,sys,time
from pathlib import Path
root=Path.cwd();py=str(root/'.venv/bin/python')
checks=[
('isolated',['SAFEACT_RUN_ISOLATED_CLUSTER_TESTS'],[py,'-m','unittest','discover','-s','tests/isolated','-t','.','-p','test_*.py'],root),
('api',['SAFEACT_RUN_API_E2E'],[py,'-m','unittest','discover','-s','tests/e2e_api','-t','.','-p','test_*.py'],root),
('agent',['SAFEACT_RUN_AGENT_HARNESS'],[py,'-m','unittest','discover','-s','tests/e2e_agent','-t','.','-p','test_*.py'],root),
('browser',[],['npx','--no-install','playwright','test'],root/'ui'),
('console-unit',[],['npm','test'],root/'ui'),
('typecheck',[],['npm','run','typecheck'],root/'ui'),
('lint',[],['npm','run','lint'],root/'ui'),
('build',[],['npm','run','build'],root/'ui')]
rows=[]
for name,flags,command,cwd in checks:
 path=Path('/tmp')/f'safeact-prestop2-final-{name}.log';started=time.monotonic()
 with path.open('x') as log:
  result=subprocess.run(command,cwd=cwd,env={**os.environ,'PYTHONPATH':str(root),**{flag:'1' for flag in flags}},stdout=log,stderr=subprocess.STDOUT)
 row=dict(check=name,exit_code=result.returncode,elapsed_seconds=time.monotonic()-started,command=command,cwd=str(cwd),enabled_flags=flags,log=str(path))
 rows.append(row);Path('/tmp/safeact-prestop2-surface-checks.json').write_text(json.dumps(rows,indent=2)+'\n')
 print(name,'exit='+str(result.returncode),f'seconds={row["elapsed_seconds"]:.1f}',flush=True)
 if result.returncode:sys.exit(result.returncode)
