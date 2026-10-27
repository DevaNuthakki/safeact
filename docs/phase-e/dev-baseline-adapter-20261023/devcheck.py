"""Run development cases only against the independently implemented B3 adapter."""

import argparse
import json
import os
import queue
import tempfile
import threading
from pathlib import Path

from chaos import coverage,oracle
from chaos.baselines.arm import B3Arm
from chaos.qualify import DEV_COUNTS,DEV_SEED,slot_worker
from chaos.scenarios import corpus
from deploy.evaluation.agent_v1.secret_scan import scan


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',required=True)
    p.add_argument('--families');p.add_argument('--slot',type=int,default=6);a=p.parse_args()
    flags=('SAFEACT_RUN_BASELINE_DEV_CHECK','SAFEACT_RUN_DB_INTEGRATION','SAFEACT_RUN_DISPOSABLE_DB_TESTS','SAFEACT_RUN_BROKER_TESTS')
    if any(os.environ.get(k)!='1' for k in flags):raise RuntimeError('Explicit local development/database/broker flags required')
    out=Path(a.out);out.mkdir(parents=True,exist_ok=False);work=queue.Queue();errors=[]
    scenarios=corpus('dev',DEV_COUNTS,DEV_SEED)
    if a.families:
        selected=set(a.families.split(','))
        if selected-set(DEV_COUNTS):raise ValueError('Existing development families only')
        scenarios=[s for s in scenarios if s.family in selected]
    for scenario in scenarios:work.put(scenario)
    with tempfile.TemporaryDirectory(prefix='safeact-b3-adapter-dev-') as temporary:
        with (out/'raw.jsonl').open('x') as raw,(out/'results.jsonl').open('x') as results:
            slot_worker(a.slot,[B3Arm],work,dict(raw=raw,results=results),threading.Lock(),Path(temporary),errors)
        private_removed=not any(Path(temporary).rglob('private-config.json'))
    rows=[json.loads(line) for line in (out/'results.jsonl').read_text().splitlines()]
    observed=set().union(*(set(r.get('interleavings',[])) for r in rows))
    required={k for k in coverage.CRITICAL if not k.startswith('agent_')}
    if a.families:required=set()  # A subset cannot assert full interleaving coverage.
    applicable=[r for r in rows if not r.get('not_applicable')]
    summary=dict(scope='Existing development cases only; no held-out/frozen qualification',
        complete_development_corpus=not bool(a.families), selected_families=a.families,
        rows=len(rows),expected_rows=len(scenarios),not_applicable=sum(r.get('not_applicable',False) for r in rows),
        not_applicable_families=sorted({r['family'] for r in rows if r.get('not_applicable')}),
        harness_errors=len(errors),safety_failure_rows=sum(oracle.safety_failures(r) for r in applicable),
        observed=sorted(observed),missing_interleavings=sorted(required-observed),private_configs_removed=private_removed,
        real_stripe_calls=0,paid_model_calls=0,final_qualification_started=False)
    (out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    if scan([out]):raise RuntimeError('Private data detected in development evidence')
    print(json.dumps(summary),flush=True)
    return int(len(rows)!=len(scenarios) or errors or summary['safety_failure_rows'] or summary['missing_interleavings'] or not private_removed)


if __name__=='__main__':raise SystemExit(main())
