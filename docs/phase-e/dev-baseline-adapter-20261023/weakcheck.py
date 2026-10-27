"""Run existing development cases only; baseline safety differences are results."""

import argparse
import json
import os
import queue
import tempfile
import threading
from pathlib import Path

from chaos import coverage, oracle
from chaos.applicability import reason
from chaos.baselines.naive_arm import B0Arm
from chaos.baselines.queue_arm import B1Arm, B2Arm
from chaos.qualify import DEV_COUNTS, DEV_SEED, exit_code, slot_worker
from chaos.scenarios import corpus
from deploy.evaluation.agent_v1.secret_scan import scan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    parser.add_argument('--arms', default='b0,b1,b2')
    parser.add_argument('--first-slot', type=int, default=4)
    args = parser.parse_args()
    flags = ('SAFEACT_RUN_BASELINE_DEV_CHECK', 'SAFEACT_RUN_DB_INTEGRATION',
             'SAFEACT_RUN_DISPOSABLE_DB_TESTS', 'SAFEACT_RUN_BROKER_TESTS')
    if any(os.environ.get(flag) != '1' for flag in flags):
        raise RuntimeError('Explicit local development/database/broker flags required')
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    scenarios = corpus('dev', DEV_COUNTS, DEV_SEED)
    available = {arm.name: arm for arm in (B0Arm, B1Arm, B2Arm)}
    names = args.arms.split(',')
    if not names or len(names) != len(set(names)) or set(names)-set(available):
        raise ValueError('Distinct development baseline arms required')
    arms = [available[name] for name in names]
    declared = {arm.name: {s.scenario_id: reason(arm, s) for s in scenarios}
                for arm in arms}
    (out/'applicability-before.json').write_text(json.dumps(declared, indent=2)+'\n')
    errors, threads, lock = [], [], threading.Lock()
    with tempfile.TemporaryDirectory(prefix='safeact-weaker-adapter-dev-') as temporary:
        with (out/'raw.jsonl').open('x') as raw, (out/'results.jsonl').open('x') as results:
            for index, arm in enumerate(arms, start=args.first_slot):
                work = queue.Queue()
                for scenario in scenarios:
                    work.put(scenario)
                thread = threading.Thread(target=slot_worker, args=(index, [arm], work,
                    dict(raw=raw, results=results), lock, Path(temporary), errors))
                threads.append(thread)
                thread.start()
            for thread in threads:
                thread.join()
        private_removed = not any(Path(temporary).rglob('private-config.json'))
    rows = [json.loads(line) for line in (out/'results.jsonl').read_text().splitlines()]
    summaries = {}
    for arm in arms:
        mine = [row for row in rows if row['arm'] == arm.name]
        applicable = [row for row in mine if not row.get('not_applicable')]
        summaries[arm.name] = dict(rows=len(mine), applicable=len(applicable),
            not_applicable=len(mine)-len(applicable),
            harness_errors=sum(bool(row.get('harness_errors')) for row in mine),
            safety_failure_rows=sum(oracle.safety_failures(row) for row in applicable),
            observed=sorted(set().union(*(set(row.get('interleavings', [])) for row in mine))))
    status = exit_code(rows, len(scenarios)*len(arms))
    summary = dict(scope='Existing development corpus only; no held-out/frozen qualification',
        arms=summaries, expected_rows=len(scenarios)*len(arms), rows=len(rows),
        harness_errors=len(errors), private_configs_removed=private_removed,
        real_stripe_calls=0, paid_model_calls=0, final_qualification_started=False,
        exit_code=status)
    (out/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    if scan([out]):
        raise RuntimeError('Private data detected in development evidence')
    print(json.dumps(summary), flush=True)
    return int(status or not private_removed)


if __name__ == '__main__':
    raise SystemExit(main())
