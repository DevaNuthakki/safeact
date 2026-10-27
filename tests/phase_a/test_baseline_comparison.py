"""Counterexamples for weaker-arm attribution, applicability and process data."""

import copy
import io
import unittest
from dataclasses import asdict
from uuid import UUID

from chaos import coverage, oracle
from chaos.applicability import reason
from chaos.baselines.arm import B3Arm
from chaos.baselines.keys import payment_key, registration_keys
from chaos.baselines.naive_arm import B0Arm
from chaos.baselines.naive_process import FRAME_LIMIT, read_frame
from chaos.baselines.queue_arm import B1Arm, B2Arm
from chaos.baselines.workflow import WorkflowRequest
from chaos.workflow_oracle import snapshots, wire_keys
from connectors.http_trace import correlation_hash
from tests.phase_a.test_workflow_qualification import fixture, lost_response_trace


def native_trace(mode='b1'):
    case, _ = fixture()
    raw = lost_response_trace()
    root = str(UUID(int=1))
    request = WorkflowRequest('north', 'requester', 'intent', case.workflow.customer_id,
        case.workflow.case_id, case.workflow.sku, case.operations[0].amount_minor,
        'payments-account', 'crm-account')
    raw.update(arm=mode)
    raw['admissions'][0]['local_id'] = root
    raw['operations'][root] = raw['operations'].pop('workflow')
    mapping = {}
    for sample in raw['database']:
        sample.update(workflow_id=root, request=asdict(request), tenant_id=request.tenant_id,
            requester_id=request.requester_id, request_key=request.request_key,
            binding_format='b0-request-v1' if mode == 'b0' else 'queue-request-v1')
        for index, step in enumerate(sample['steps'], start=2):
            role = step['step']
            operation = str(UUID(int=index))
            key = operation if role == 'order' else payment_key(operation, 1, mode)
            digest = correlation_hash(key)
            mapping[correlation_hash(role)] = digest
            step.update(operation_id=operation, native_key_mode=mode,
                attempt_ordinals=[1], correlation_sha256=digest, correlation_sha256s=[digest],
                tenant_id=request.tenant_id, requester_id=request.requester_id,
                request_key=f'wf:{root}:{role}',
                provider_account_id='crm-account' if role == 'order' else 'payments-account',
                applied_reference='charge-effect' if role == 'charge' else None)
            if role == 'compensation':
                step.update(compensates_operation_id=str(UUID(int=2)), resource_id='charge-effect')
            if mode == 'b0':
                step['evidence_rows'] = 0
    for event in raw['ledger'] + raw['http']:
        event['correlation_sha256'] = mapping[event['correlation_sha256']]
    if mode == 'b0':
        raw['operations'][root]['evidence_rows'] = 0
        raw['infrastructure'] = dict(protocol='b0-process-v1', database_created=False,
            processes=[dict(event='b0_process_started', pid=321, monotonic_ns=1)],
            acceptance=[dict(source='process', event='b0_acceptance_observed', durable=False,
                operation_id=root, pid=321, sample_started_ns=2, sample_finished_ns=3,
                monotonic_ns=3)], process_snapshots=raw.pop('database'))
        for sample in raw['infrastructure']['process_snapshots']:
            sample.update(source='process', event='process_workflow_sample', sender_pid=321)
        raw['database'] = []
    return case, raw


class BaselineComparisonTests(unittest.TestCase):
    def test_fresh_payment_keys_do_not_discard_prior_attempt_effects(self):
        case, raw = native_trace()
        latest = raw['database'][-1]
        refund = latest['steps'][-1]
        previous = refund['correlation_sha256']
        second = correlation_hash(payment_key(refund['operation_id'], 2, 'b1'))
        refund.update(attempt_ordinals=[1, 2], correlation_sha256=second,
            correlation_sha256s=[previous, second])
        for event in copy.deepcopy(raw['ledger']):
            if event.get('correlation_sha256') == previous and event['event'] in ('provider_start', 'call', 'effect'):
                event.update(correlation_sha256=second, monotonic_ns=event['monotonic_ns']+30)
                if event['event'] == 'provider_start':
                    event['provider_call_id'] = 'refund-retry'
                if event['event'] == 'effect':
                    event['effect_id'] = 'second-refund'
                raw['ledger'].append(event)
        latest.update(sample_started_ns=89, sample_finished_ns=90, monotonic_ns=90)
        result = oracle.evaluate(case, raw)
        self.assertEqual((result['compensation_effects'], result['compensation_duplicates']), (2, 1))
        self.assertEqual((result['orphan_effects'], result['invalid_compensation_effects']), (0, 0))
        self.assertTrue(oracle.safety_failures(result))
        self.assertIn('compensation_applied_response_lost', coverage.observed(raw))

    def test_aliases_cannot_import_foreign_effects_or_another_arm_strategy(self):
        for mutation in ('foreign', 'mode', 'ordinal', 'primary', 'duplicate'):
            case, raw = native_trace()
            refund = raw['database'][-1]['steps'][-1]
            if mutation == 'foreign':
                refund['correlation_sha256s'].append('foreign')
            elif mutation == 'mode':
                refund['native_key_mode'] = 'b2'
            elif mutation == 'ordinal':
                refund['attempt_ordinals'] = [1, 3]
            elif mutation == 'primary':
                refund['correlation_sha256'] = 'foreign'
            else:
                refund['correlation_sha256s'] *= 2
            with self.subTest(mutation=mutation):
                result = oracle.evaluate(case, raw)
                self.assertTrue(result['integrity'])
                self.assertFalse(result['operations'][0]['safe_completion'])
                self.assertNotIn('compensation_applied_response_lost', coverage.observed(raw))

    def test_no_database_rows_are_invented_for_memory_only_workflow(self):
        case, raw = native_trace('b0')
        result = oracle.evaluate(case, raw)
        self.assertFalse(oracle.safety_failures(result), result)
        self.assertEqual(result['operations'][0]['evidence_rows'], 0)
        self.assertEqual(raw['database'], [])
        self.assertIn('compensation_applied_response_lost', coverage.observed(raw))

    def test_memory_bindings_need_actual_owned_process_and_prior_acceptance(self):
        for mutation in ('protocol', 'database', 'owner', 'acceptance', 'pid', 'time', 'root'):
            case, raw = native_trace('b0')
            infra = raw['infrastructure']
            if mutation == 'protocol':
                infra['protocol'] = 'b3-registration-v1'
            elif mutation == 'database':
                infra['database_created'] = True
            elif mutation == 'owner':
                infra['processes'] = []
            elif mutation == 'acceptance':
                infra['acceptance'][0]['durable'] = True
            elif mutation == 'pid':
                for sample in infra['process_snapshots']:
                    sample['sender_pid'] += 1
            elif mutation == 'root':
                infra['acceptance'][0]['operation_id'] = str(UUID(int=999))
            else:
                infra['acceptance'][0]['sample_finished_ns'] = 200
            with self.subTest(mutation=mutation):
                self.assertEqual(snapshots(raw, raw['admissions'][0]['local_id']), [])
                result = oracle.evaluate(case, raw)
                self.assertTrue(result['harness_errors'])
                self.assertEqual(result['unaccounted'], 1)

    def test_native_key_coverage_is_bounded_and_provider_specific(self):
        op = str(UUID(int=7))
        for mode in ('b0', 'b1'):
            keys = registration_keys(op, 'mock-payments', 'q-v2', mode)
            self.assertEqual(len(keys), 3)
            self.assertEqual(len(set(keys)), 3)
        for mode in ('b2', 'b3'):
            self.assertEqual(payment_key(op, 1, mode), payment_key(op, 3, mode))
            self.assertEqual(len(registration_keys(op, 'mock-payments', 'q-v2', mode)), 1)
        for connector in ('mock-crm', 'mock-email'):
            self.assertEqual(registration_keys(op, connector, 'q-v2', 'b2'), ())

    def test_process_frames_reject_truncation_oversize_duplicate_keys_and_nonobjects(self):
        for text in ('', '{}', '[]\n', '{"x":1,"x":2}\n', ' '*FRAME_LIMIT+'{}\n'):
            with self.subTest(length=len(text)), self.assertRaises(ValueError):
                read_frame(io.StringIO(text))
        self.assertEqual(read_frame(io.StringIO('{"kind":"ack"}\n')), {'kind': 'ack'})

    def test_applicability_comes_from_absent_mechanisms_and_keeps_weak_guards_testable(self):
        from chaos.scenarios import generate
        for arm in (B0Arm, B1Arm, B2Arm, B3Arm):
            self.assertIsNone(reason(arm, generate('dev', 'F10', 0, 7)))
        for arm in (B0Arm, B1Arm, B2Arm):
            self.assertIsNotNone(reason(arm, generate('dev', 'F12', 0, 7)))
            self.assertIsNotNone(reason(arm, generate('dev', 'F14', 0, 7)))
            self.assertIsNotNone(reason(arm, generate('dev', 'F15', 0, 7)))
        self.assertIsNotNone(reason(B0Arm, generate('dev', 'F08', 0, 7)))
        self.assertIsNone(reason(B1Arm, generate('dev', 'F08', 0, 7)))
        self.assertIsNone(reason(B3Arm, generate('dev', 'F12', 0, 7)))

    def test_queue_redelivery_proof_is_explicit_and_cannot_relax_safeact_observations(self):
        from tests.phase_a.test_broker_redelivery import evidence
        raw = evidence()
        raw['arm'] = 'b1'
        for sample in raw['database']:
            sample.update(event='db_broker_inflight_attempt', proof_format='queue-attempt-v1',
                attempt_outcome_pending=True, native_job_state='SUCCEEDED')
        label = 'broker_redelivered_during_dispatch'
        self.assertIn(label, coverage.observed(raw))
        for mutation in ('arm', 'pending', 'lease', 'identity', 'time'):
            altered = copy.deepcopy(raw)
            if mutation == 'arm':
                altered['arm'] = 'safeact'
            elif mutation == 'pending':
                altered['database'][-1]['attempt_outcome_pending'] = False
            elif mutation == 'lease':
                altered['database'][-1]['lease_active'] = False
            elif mutation == 'identity':
                altered['database'][-1]['attempt_id'] = 'another-attempt'
            else:
                altered['database'][-1]['sample_started_ns'] = 40
            with self.subTest(mutation=mutation):
                self.assertNotIn(label, coverage.observed(altered))


if __name__ == '__main__':
    unittest.main()
