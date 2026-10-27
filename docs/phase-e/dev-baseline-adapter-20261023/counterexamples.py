"""Corrupt saved observations only; no sockets, database or scenario execution."""

import argparse
import copy
import importlib.util
import json
from pathlib import Path

from chaos import coverage, oracle


def check(root):
    spec = importlib.util.spec_from_file_location('comparison_replay', root/'replay.py')
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    selected, _ = replay.records(root)
    raw = lambda arm, family, index: selected[(f'D-{family}-{index:04d}', arm)][1]
    rejected = []

    def test(name, base, mutate, refuses):
        changed = copy.deepcopy(base)
        mutate(changed)
        assert refuses(changed), name
        rejected.append(name)

    def workflow_steps(value, role):
        return [step for sample in value.get('database', []) if sample.get('event') == 'db_workflow_sample'
                for step in sample['steps'] if step['step'] == role]

    def invalid(value):
        row = replay.evaluate(value)
        return bool(row.get('harness_errors') or row.get('integrity'))

    b2 = raw('b2', 'F13', 0)
    assert not oracle.safety_failures(replay.evaluate(b2))
    for name, change in (
        ('foreign_alias', lambda step: step['correlation_sha256s'].append('foreign')),
        ('other_arm_key_strategy', lambda step: step.update(native_key_mode='b1')),
        ('attempt_ordinal_gap', lambda step: step.update(attempt_ordinals=[1,3])),
        ('wrong_primary_identity', lambda step: step.update(correlation_sha256='foreign')),
        ('wrong_inverse_target', lambda step: step['payload'].update(charge_id='unrelated-charge')),
        ('inverse_before_confirmed_order_failure', lambda step: step.update(state='UNKNOWN'))):
        role = 'order' if name == 'inverse_before_confirmed_order_failure' else 'compensation'
        test(name, b2, lambda value, role=role, change=change: [change(step) for step in workflow_steps(value, role)], invalid)
    test('queue_snapshot_cannot_masquerade_as_b3', b2, lambda value: value.update(arm='b3'), invalid)

    b0 = raw('b0', 'F13', 0)
    assert not replay.evaluate(b0)['harness_errors']
    for name, change in (
        ('memory_snapshot_without_observed_acceptance', lambda value: value['infrastructure'].update(acceptance=[])),
        ('memory_snapshot_with_invented_database', lambda value: value['infrastructure'].update(database_created=True))):
        test(name, b0, change, lambda value: bool(replay.evaluate(value)['harness_errors'])
            and replay.evaluate(value)['unaccounted'] == 1)

    concurrent = next(value for (_, arm), (_, value) in selected.items() if arm == 'safeact'
        and coverage.boundary_details(value)['concurrent_reclaim_refused'])
    for name, change in (
        ('missing_concurrent_return', lambda value: [event.update(task_status='recorded') for event in value['broker']
            if event.get('event') == 'task_returned' and event.get('redelivered')]),
        ('expired_original_fence', lambda value: [event.update(lease_active=False) for event in value['database']
            if event.get('event') == 'db_broker_dispatching']),
        ('weaker_arm_cannot_claim_fenced_refusal', lambda value: value.update(arm='b1'))):
        test(name, concurrent, change, lambda value: not coverage.boundary_details(value)['concurrent_reclaim_refused'])

    for stage in ('claim', 'fence', 'outcome'):
        committed = next(value for (_, arm), (_, value) in selected.items() if arm == 'safeact'
            and stage in coverage.boundary_details(value)['commit_ack_stages'])
        for mutation in ('foreign_xid', 'missing_independent_commit'):
            def change(value, mutation=mutation):
                if mutation == 'foreign_xid':
                    for event in value['execution']:
                        if event.get('event') == 'hook_error':
                            event['commit_xid'] = '99999999999999'
                else:
                    value['database'] = [event for event in value['database'] if event.get('event') != 'db_audit_committed']
            test(stage+'_'+mutation, committed, change,
                lambda value, stage=stage: stage not in coverage.boundary_details(value)['commit_ack_stages'])

    label = 'breaker_open_with_unresolved_work'
    breaker = raw('safeact', 'F16', 0)
    assert label in coverage.observed(breaker)
    for name, updates in (('closed_breaker', dict(breaker_open=False)),
                          ('insufficient_breaker_failures', dict(consecutive_failures=0)),
                          ('resolved_work_does_not_prove_pending_recovery', dict(state='SUCCEEDED')),
                          ('foreign_provider_breaker', dict(provider='email'))):
        test(name, breaker, lambda value, updates=updates: [event.update(updates) for event in value['database']
                if event.get('event') == 'db_breaker_sample'],
            lambda value: label not in coverage.observed(value))

    for index in range(3):
        review = raw('b3', 'F14', index)
        assert not replay.evaluate(review)['harness_errors']
        test('native_review_missing_http_'+str(index), review, lambda value: value.update(boundary=[]),
             lambda value: bool(replay.evaluate(value)['harness_errors']))
    return dict(scope='Saved-data counterexamples only; no execution', cases=len(rejected),
        rejected=len(rejected), mutations=rejected, final_qualification_started=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root')
    parser.add_argument('--out')
    args = parser.parse_args()
    value = check(Path(args.root))
    if args.out:
        Path(args.out).write_text(json.dumps(value, indent=2)+'\n')
    print('Rejected', value['rejected'], 'of', value['cases'], 'saved-data mutations')
