"""Replay retained development bytes; never execute a scenario or provider."""

import argparse
import json
import tarfile
from pathlib import Path

from chaos import coverage, oracle, scoring
from chaos.applicability import critical_labels, reason
from chaos.arms.safeact import SafeActArm
from chaos.arms.baselines import ARMS
from chaos.frozen import sha
from chaos.qualify import DEV_COUNTS, DEV_SEED
from chaos.scenarios import Scenario, corpus


PRIORITY = ('b3-first', 'weaker-first', 'weaker-marker-corrected', 'safeact-first',
            'b3-audit', 'breaker', 'b1-lifecycle-corrected', 'workflow-accounting')


def records(root):
    files = json.loads((root/'development-run-files.json').read_text())['files']
    expected = {entry['path']: entry['sha256'] for entry in files}
    with tarfile.open(root/'development-runs.tar.gz') as archive:
        members = archive.getmembers()
        assert len(members) == len({member.name for member in members})
        assert {member.name for member in members} == set(expected)
        data = {}
        for member in members:
            assert member.isfile() and not member.name.startswith('/') and '..' not in Path(member.name).parts
            value = archive.extractfile(member).read()
            assert sha(value) == expected[member.name]
            data[member.name] = value
    selected, retained = {}, []
    for label in PRIORITY:
        rows = [json.loads(line) for line in data[label+'/raw.jsonl'].splitlines()]
        results = [json.loads(line) for line in data[label+'/results.jsonl'].splitlines()]
        assert len(rows) == len(results)
        assert {(row['scenario_id'], row['arm']) for row in rows} == {
            (row['scenario_id'], row['arm']) for row in results}
        for raw in rows:
            retained.append((label, raw))
            selected[(raw['scenario_id'], raw['arm'])] = (label, raw)
    return selected, retained


def evaluate(raw):
    case = Scenario.from_json(json.dumps(raw['scenario']))
    if raw.get('not_applicable'):
        classes = {'safeact': SafeActArm, **ARMS}
        return dict(scenario_id=case.scenario_id, arm=raw['arm'], family=case.family,
            connector=case.connector, completion_achievable=case.completion_achievable,
            expected_operations=len(case.operations), not_applicable=True,
            not_applicable_reason=raw.get('not_applicable_reason') or reason(classes[raw['arm']], case),
            reason_source='retained declaration' if raw.get('not_applicable_reason') else 'current declaration for historical N/A')
    row = oracle.evaluate(case, raw)
    row['interleavings'] = sorted(coverage.observed(raw))
    return row


def check(root):
    selected, retained = records(root)
    cases = corpus('dev', DEV_COUNTS, DEV_SEED)
    classes = {'safeact': SafeActArm, **ARMS}
    assert set(selected) == {(case.scenario_id, arm) for case in cases for arm in classes}
    rows = []
    for case in cases:
        for arm in classes:
            label, raw = selected[(case.scenario_id, arm)]
            assert Scenario.from_json(json.dumps(raw['scenario'])) == case
            assert bool(raw.get('not_applicable')) == (reason(classes[arm], case) is not None)
            row = evaluate(raw)
            assert not row.get('harness_errors') and not row.get('integrity')
            if not row.get('not_applicable'):
                assert raw['quiescence']['complete'] and not raw.get('cleanup_incomplete')
            row['development_source'] = label
            rows.append(row)
    seen = {arm: set() for arm in classes}
    details = {arm: dict(commit_ack_stages=set(), concurrent_reclaim_refused=False) for arm in classes}
    diagnostics = []
    for label, raw in retained:
        if raw.get('harness_errors') or raw.get('cleanup_incomplete'):
            diagnostics.append(dict(source=label, scenario_id=raw['scenario_id'], arm=raw['arm'],
                errors=len(raw.get('harness_errors', []))))
            continue
        if raw.get('not_applicable'):
            continue
        seen[raw['arm']].update(coverage.observed(raw))
        detail = coverage.boundary_details(raw)
        details[raw['arm']]['commit_ack_stages'].update(detail['commit_ack_stages'])
        details[raw['arm']]['concurrent_reclaim_refused'] |= detail['concurrent_reclaim_refused']
    missing = {arm: sorted(critical_labels(classes[arm])-labels) for arm, labels in seen.items()}
    assert not missing['safeact'] and not missing['b3'], missing
    for arm in ('safeact', 'b3'):
        assert {'claim','fence','outcome'} <= details[arm]['commit_ack_stages']
        assert details[arm]['concurrent_reclaim_refused']
    for detail in details.values():
        detail['commit_ack_stages'] = sorted(detail['commit_ack_stages'])
    summary = scoring.summarize(rows)
    summary.update(scope='Selected development instrumentation captures; runs span development revisions; not a frozen comparison',
        selected_rows=len(rows), retained_rows=len(retained), retained_diagnostic_rows=diagnostics,
        source_priority=PRIORITY, coverage_basis='Union of retained development rows with no harness/cleanup errors',
        observed={arm: sorted(labels) for arm, labels in seen.items()}, missing=missing,
        boundary_details=details, frozen_qualification_executed=False, real_stripe_calls=0, paid_model_calls=0)
    return rows, summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root')
    parser.add_argument('--out')
    args = parser.parse_args()
    rows, summary = check(Path(args.root))
    if args.out:
        out = Path(args.out)
        out.mkdir(exist_ok=False)
        (out/'selected-results.jsonl').write_text('\n'.join(json.dumps(row) for row in rows)+'\n')
        (out/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    print('Replayed', len(rows), 'selected development rows; all applicable SafeAct/B3 labels and native boundaries observed')
