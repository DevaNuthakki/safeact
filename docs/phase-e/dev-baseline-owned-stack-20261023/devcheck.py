"""Retain selected owned-stack checks; no comparison or qualification."""

import argparse
import json
import os
import sys
import time
import unittest
from pathlib import Path

from chaos.quiescence import process_group_exists
from deploy.evaluation.agent_v1.secret_scan import scan_text
from tests.integration.test_baseline_stack import BaselineStackTests


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',required=True);a=p.parse_args()
    if any(os.environ.get(k)!='1' for k in ('SAFEACT_RUN_BASELINE_DEV_CHECK','SAFEACT_RUN_DB_INTEGRATION',
        'SAFEACT_RUN_DISPOSABLE_DB_TESTS','SAFEACT_RUN_BROKER_TESTS')):
        raise RuntimeError('Explicit local disposable/database/broker development flags required')
    out=Path(a.out);out.mkdir(parents=True,exist_ok=False);rows=[]
    for name in unittest.defaultTestLoader.getTestCaseNames(BaselineStackTests):
        case=BaselineStackTests(name);result=unittest.TestResult();started=time.monotonic();case.run(result)
        stack=case.stack
        cleanup=dict(schema_dropped=not stack.env.created,private_config_removed=not stack.config_path.exists(),
            private_directory_removed=not Path(case.private.name).exists(),controller_stopped=not stack.controller.thread.is_alive(),
            processes_stopped=all(g['process'].poll() is not None and not process_group_exists(g['process'].pid) for g in stack.generations))
        value=dict(case=name,scope='Owned B3 stack development check; not qualification or family coverage',
            passed=result.wasSuccessful() and not result.skipped,failures=[t for _,t in result.failures+result.errors],
            skipped=result.skipped,raw=case.raw,elapsed_seconds=time.monotonic()-started,cleanup=cleanup)
        text=json.dumps(value,indent=2,default=str)+'\n'
        if stack.env.password in text or scan_text(text,suffix='.json'):
            raise RuntimeError('Private data in observations; refusing retention')
        (out/(name+'.json')).write_text(text);rows.append(value)
        print(name,'PASS' if value['passed'] else 'FAIL',flush=True)
    summary=dict(scope='Local owned-stack development, no frozen family coverage or cross-arm comparison',
        cases=len(rows),passed=sum(r['passed'] for r in rows),cleanup_complete=all(all(r['cleanup'].values()) for r in rows),
        expected_integrity_refusals=sum(r['raw']['declared_integrity_failure'] for r in rows),
        effects=sum(sum(e['event']=='effect' for e in r['raw']['ledger']) for r in rows),
        http_dispatches=sum(sum(e['event']=='http_start' and e['method']=='POST' for e in r['raw']['http']) for r in rows),
        real_stripe_calls=0,paid_model_calls=0,final_qualification_started=False)
    (out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary),flush=True)
    return int(summary['cases']!=summary['passed'] or not summary['cleanup_complete'])


if __name__=='__main__':sys.exit(main())
