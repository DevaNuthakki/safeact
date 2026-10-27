"""Observe existing F16 development faults with independent breaker samples."""

import argparse
import json
import os
import queue
import tempfile
import threading
from pathlib import Path

from chaos.arms.safeact import SafeActArm
from chaos.baselines.arm import B3Arm
from chaos.baselines.queue_arm import B1Arm, B2Arm
from chaos.qualify import DEV_COUNTS, DEV_SEED, exit_code, slot_worker
from chaos.scenarios import corpus
from deploy.evaluation.agent_v1.secret_scan import scan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    if any(os.environ.get(flag) != '1' for flag in ('SAFEACT_RUN_BASELINE_DEV_CHECK',
            'SAFEACT_RUN_DB_INTEGRATION', 'SAFEACT_RUN_DISPOSABLE_DB_TESTS', 'SAFEACT_RUN_BROKER_TESTS')):
        raise RuntimeError('Explicit local development/database/broker flags required')
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    cases = corpus('dev', {'F16': DEV_COUNTS['F16']}, DEV_SEED)
    arms = [SafeActArm, B1Arm, B2Arm, B3Arm]
    errors, lock, threads = [], threading.Lock(), []
    with tempfile.TemporaryDirectory(prefix='safeact-pacing-development-') as temporary:
        with (out/'raw.jsonl').open('x') as raw, (out/'results.jsonl').open('x') as results:
            for slot, arm in enumerate(arms, start=1):
                work = queue.Queue()
                for case in cases:
                    work.put(case)
                thread = threading.Thread(target=slot_worker, args=(slot, [arm], work,
                    dict(raw=raw, results=results), lock, Path(temporary), errors))
                threads.append(thread)
                thread.start()
            for thread in threads:
                thread.join()
        removed = not any(Path(temporary).rglob('private-config.json'))
    rows = [json.loads(line) for line in (out/'results.jsonl').read_text().splitlines()]
    label = 'breaker_open_with_unresolved_work'
    credited = {arm.name: any(label in row.get('interleavings', []) for row in rows
        if row['arm'] == arm.name) for arm in arms}
    status = exit_code(rows, 20)
    summary = dict(scope='Existing F16 development only; no frozen/held-out execution',
        rows=len(rows), expected_rows=20, harness_errors=len(errors), observed_breaker=credited,
        private_configs_removed=removed, final_qualification_started=False,
        real_stripe_calls=0, paid_model_calls=0)
    (out/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    if scan([out]):
        raise RuntimeError('Private data detected in development evidence')
    print(json.dumps(summary), flush=True)
    return int(status or not removed or not all(credited.values()))


if __name__ == '__main__':
    raise SystemExit(main())
