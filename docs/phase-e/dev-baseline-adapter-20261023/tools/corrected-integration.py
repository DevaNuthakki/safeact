import json,os,subprocess,sys,time
from pathlib import Path
root=Path.cwd();py=str(root/'.venv/bin/python')
original=Path('/tmp/safeact-prestop2-final-integration.log').read_text()
assert 'FAILED (errors=1)' in original and 'ValueError: Unexpected owned queue' in original
results=[]
for name,command,log_path in [
 ('broker-protocol',[py,'-m','unittest','tests.integration.test_broker_trace_signals'],'/tmp/safeact-prestop2-final-broker-protocol-corrected.log'),
 ('integration',[py,'/tmp/safeact-prestop2-full-integration-corrected-runner.py'],'/tmp/safeact-prestop2-final-integration-corrected.log')]:
 start=time.monotonic()
 with Path(log_path).open('x') as log:
  result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT)
 row=dict(check=name,exit_code=result.returncode,elapsed_seconds=time.monotonic()-start,command=command,log=log_path)
 results.append(row);Path('/tmp/safeact-prestop2-corrected-integration-checks.json').write_text(json.dumps(results,indent=2)+'\n')
 print(name,'exit='+str(result.returncode),f'seconds={row["elapsed_seconds"]:.1f}',flush=True)
 if result.returncode:sys.exit(result.returncode)
