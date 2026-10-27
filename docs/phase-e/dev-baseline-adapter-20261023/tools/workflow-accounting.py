import json,os,queue,tempfile,threading
from pathlib import Path
from chaos.arms.safeact import SafeActArm
from chaos.baselines.arm import B3Arm
from chaos.qualify import slot_worker,exit_code
from chaos.scenarios import generate
from deploy.evaluation.agent_v1.secret_scan import scan
assert all(os.environ.get(k)=='1' for k in ('SAFEACT_RUN_BASELINE_DEV_CHECK','SAFEACT_RUN_DB_INTEGRATION','SAFEACT_RUN_DISPOSABLE_DB_TESTS','SAFEACT_RUN_BROKER_TESTS'))
out=Path('/tmp/safeact-prestop2-workflow-accounting-development');out.mkdir(exist_ok=False)
errors=[];threads=[];lock=threading.Lock();case=generate('dev','F13',2,7)
with tempfile.TemporaryDirectory(prefix='safeact-workflow-accounting-dev-') as temporary:
 with (out/'raw.jsonl').open('x') as raw,(out/'results.jsonl').open('x') as results:
  for slot,arm in enumerate((SafeActArm,B3Arm)):
   work=queue.Queue();work.put(case)
   thread=threading.Thread(target=slot_worker,args=(slot,[arm],work,dict(raw=raw,results=results),lock,Path(temporary),errors));thread.start();threads.append(thread)
  for thread in threads:thread.join()
rows=[json.loads(line) for line in (out/'results.jsonl').read_text().splitlines()]
assert exit_code(rows,2)==0 and not errors
assert all(row['operations'][0]['outcome_class']=='pending' and row['compensation_effects']==0 for row in rows)
assert not scan([out])
summary=dict(scope='Rejected-inverse workflow accounting development; no frozen or held-out execution',rows=len(rows),harness_errors=0,actual_escalations_invented=0,final_qualification_started=False,real_stripe_calls=0,paid_model_calls=0)
(out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary),flush=True)
