"""Construct and hash comparison inputs without running a single scenario."""

import argparse
import json
from pathlib import Path

from chaos import coverage, scoring
from chaos.applicability import critical_labels, reason
from chaos.arms.safeact import SafeActArm
from chaos.baselines.arm import B3Arm
from chaos.baselines.naive_arm import B0Arm
from chaos.baselines.queue_arm import B1Arm, B2Arm
from chaos.frozen import ARMS, COUNTS, ROOT, SEED, scenarios, sha, smoke_ids, source_paths
from chaos.profiles import QUAL_PROFILES
from chaos.scenarios import COMBINATIONS, digest


def build(root=ROOT, *, replace=False):
    ledger = root/'docs/phase-e/qual-v1/executions'
    if replace and ledger.exists() and any(ledger.iterdir()):
        raise ValueError('Frozen inputs were executed; preserve them and make a disclosed new freeze')
    cases = scenarios()
    arms = [SafeActArm, B0Arm, B1Arm, B2Arm, B3Arm]
    contracts = [dict(contract=profile.row(), limits=(profile.limits.row(profile.connector_id, profile.version)
        if profile.limits else None)) for profile in QUAL_PROFILES]
    artifacts = {
        'scenarios': ('chaos/scenarios_qual_v1.jsonl', '\n'.join(case.to_json() for case in cases)+'\n'),
        'contracts': ('chaos/contracts_qual_v1.json', contracts),
        'combinations': ('chaos/combinations.json', dict(families=COMBINATIONS,
            cases_per_family=20, executed_during_development=False)),
        'coverage': ('chaos/coverage_qual_v1.json', dict(critical=coverage.CRITICAL,
            applicable={arm.name: sorted(critical_labels(arm)) for arm in arms},
            native_commit_ack_stages=dict(safeact=['claim','fence','outcome'], b3=['claim','fence','outcome'],
                b1=['delivery','outcome'], b2=['delivery','outcome'], b0=[]),
            concurrent_fenced_refusal_required=['safeact','b3'])),
        'applicability': ('chaos/applicability_qual_v1.json', {arm.name:
            {case.scenario_id: absent for case in cases if (absent := reason(arm, case)) is not None} for arm in arms}),
        'source': ('chaos/source_qual_v1.json', dict(files=[dict(path=name, sha256=sha((root/name).read_bytes()))
            for name in source_paths(root)])),
    }
    manifest_path = root/'chaos/manifest_qual_v1.json'
    pin_path = root/'chaos/qualification_pin.py'
    if not replace and (manifest_path.exists() or any((root/name).exists() for name, _ in artifacts.values())):
        raise ValueError('Candidate inputs already exist; do not overwrite a reviewed freeze')
    for name, value in artifacts.values():
        (root/name).write_text(value if isinstance(value, str) else json.dumps(value, indent=2)+'\n')
    inputs = {key: dict(path=name, sha256=sha((root/name).read_bytes())) for key, (name, _) in artifacts.items()}
    for key, name in (('timing', 'chaos/profiles.py'), ('scoring', 'chaos/scoring.py'), ('oracle', 'chaos/oracle.py'),
                      ('workflow_oracle', 'chaos/workflow_oracle.py'), ('generator', 'chaos/scenarios.py')):
        inputs[key] = dict(path=name, sha256=sha((root/name).read_bytes()))
    manifest = dict(format='safeact-qual-v1', version='qual_v1', review_status='awaiting_STOP_2',
        scope='Frozen inputs only; no qualification scenario has executed', seed=SEED, family_counts=COUNTS,
        scenarios=1200, arms=ARMS, corpus_digest=digest(cases), smoke_ids=smoke_ids(cases), inputs=inputs,
        scoring_version=scoring.SCORING_VERSION, headline='Primary originals only; diagnostics never replace failures',
        budget=dict(primary_scenario_arms=6000, wall_seconds=86400, maximum_slots=7,
            primary_invocations=1, execution_ledger_dir='docs/phase-e/qual-v1/executions',
            external_provider_cost_usd=0, paid_model_cost_usd=0),
        calibration=dict(candidates=[1,2,4,6,7], full_slots=None, phase='After STOP 2; before full outcomes',
            selection='Highest candidate with every sampled one-minute load strictly below CPU count; outcomes unused'),
        load_suspect=dict(families=['F05','F06','F07','F08','F11','F16'],
            above_cpu_count=True, observation_overrun_seconds=1, headline_replacement=False),
        environment=dict(per_scenario=['load_1','load_5','load_15','available_memory_bytes'],
            every_frozen_run=['refuses battery or unknown power before claiming the invocation',
                'starts its own caffeinate -i -w <pid> on macOS and requires its pmset assertion',
                'records power_source, caffeinate (start) and caffeinate_end'],
            laptop_full_run=['same calibrated machine and OS']),
        production_execution=False, stripe_calls=0, paid_model_calls=0)
    manifest_path.write_text(json.dumps(manifest, indent=2)+'\n')
    pinned = sha(manifest_path.read_bytes())
    pin_path.write_text('"""Reviewed candidate input anchor; never a runtime credential."""\n\n'
        +f'PINNED_SHA256 = "{pinned}"\nPINNED = PINNED_SHA256\n')
    return manifest, pinned


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replace-unreviewed', action='store_true',
        help='Replace a development candidate before STOP 2; never a reviewed/run comparison')
    args = parser.parse_args()
    manifest, pinned = build(replace=args.replace_unreviewed)
    print(json.dumps(dict(scenarios=manifest['scenarios'], smoke=len(manifest['smoke_ids']),
        manifest_sha256=pinned, qualification_executed=False)))


if __name__ == '__main__':
    main()
