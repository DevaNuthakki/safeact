"""Ordinary non-qualification regressions. Run after database integration ends."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

root=Path.cwd();out=Path('/tmp/safeact-pool-integration-20261024');py=str(root/'.venv/bin/python')
checks=[
 ('isolated',['SAFEACT_RUN_ISOLATED_CLUSTER_TESTS'],[py,'-m','unittest','discover','-s','tests/isolated','-t','.','-p','test_*.py'],root),
 ('api',['SAFEACT_RUN_API_E2E'],[py,'-m','unittest','discover','-s','tests/e2e_api','-t','.','-p','test_*.py'],root),
 ('agent',['SAFEACT_RUN_AGENT_HARNESS'],[py,'-m','unittest','discover','-s','tests/e2e_agent','-t','.','-p','test_*.py'],root),
 ('browser',[],['npx','--no-install','playwright','test'],root/'ui'),
 ('console-unit',[],['npm','test'],root/'ui'),
 ('typecheck',[],['npm','run','typecheck'],root/'ui'),
 ('lint',[],['npm','run','lint'],root/'ui'),
 ('build',[],['npm','run','build'],root/'ui'),
]
records=[]
for name,flags,command,cwd in checks:
    started=time.monotonic();log=out/('final-'+name+'.log')
    with log.open('x') as stream:
        result=subprocess.run(command,cwd=cwd,
            env={**os.environ,'PYTHONPATH':str(root),**{flag:'1' for flag in flags}},
            stdout=stream,stderr=subprocess.STDOUT)
    row=dict(check=name,exit_code=result.returncode,elapsed_seconds=time.monotonic()-started,
        command=command,enabled_flags=flags,log=log.name)
    records.append(row);(out/'surface-checks.json').write_text(json.dumps(records,indent=2)+'\n')
    print(name,'exit='+str(result.returncode),f'seconds={row["elapsed_seconds"]:.1f}',flush=True)
    if result.returncode:sys.exit(result.returncode)
