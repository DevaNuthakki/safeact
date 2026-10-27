"""Retain selected local B3 observer checks; no qualification arm is registered."""

import argparse
import json
import os
import sys
import time
import unittest
from dataclasses import asdict
from pathlib import Path

from chaos import oracle
from deploy.evaluation.agent_v1.secret_scan import scan_text
from tests.integration.test_baseline_observer import BaselineObserverTests


CASES = (
    'test_actual_normal_workflow_passes_the_common_oracle',
    'test_actual_inverse_with_distinct_provider_accounts_passes_the_common_oracle',
    'test_actual_lost_inverse_reply_requires_matching_lookup_before_restoration',
    'test_actual_hidden_inverse_stays_unresolved_and_preserves_effect',
    'test_actual_rejected_inverse_does_not_claim_restoration',
)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out',required=True)
    args=parser.parse_args()
    if any(os.environ.get(k)!='1' for k in ('SAFEACT_RUN_BASELINE_DEV_CHECK','SAFEACT_RUN_DB_INTEGRATION',
                                           'SAFEACT_RUN_DISPOSABLE_DB_TESTS')):
        raise RuntimeError('Explicit disposable/local development opt-ins required')
    out=Path(args.out);out.mkdir(parents=True,exist_ok=False);rows=[]
    for name in CASES:
        case=BaselineObserverTests(name);result=unittest.TestResult();start=time.monotonic();case.run(result)
        raw=getattr(case,'raw',{})
        if raw:raw['scenario']=asdict(case.case)
        value=dict(case=name,raw=raw,passed=result.wasSuccessful() and not result.skipped,
            failures=[trace for _,trace in result.failures+result.errors],skipped=result.skipped,
            elapsed_seconds=time.monotonic()-start,cleanup_complete=not case.env.created,
            result=oracle.evaluate(case.case,raw) if raw else None)
        text=json.dumps(value,indent=2,default=str)+'\n'
        if case.env.password in text or scan_text(text,suffix='.json'):
            raise RuntimeError('Private data found; refusing evidence retention')
        (out/(name+'.json')).write_text(text);rows.append(value)
        print(name,'PASS' if value['passed'] else 'FAIL',flush=True)
    summary=dict(scope='Selected B3 database adapter development checks; not qualification or cross-arm comparison',
        cases=len(rows),passed=sum(r['passed'] for r in rows),cleanup_complete=all(r['cleanup_complete'] for r in rows),
        forward_effects=sum(r['result']['forward_effects'] for r in rows),
        inverse_effects=sum(r['result']['compensation_effects'] for r in rows),
        safety_failures=sum(bool(oracle.safety_failures(r['result'])) for r in rows),
        real_stripe_calls=0,paid_model_calls=0,final_qualification_started=False)
    (out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary),flush=True)
    return int(summary['passed']!=len(rows) or not summary['cleanup_complete'] or summary['safety_failures'])


if __name__=='__main__':sys.exit(main())
