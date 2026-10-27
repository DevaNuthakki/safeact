"""Direct breaker, commit-stage and concurrent-refusal proof counterexamples."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from chaos import coverage
from chaos.database_trace import breaker_rows
from tests.phase_a.test_broker_redelivery import evidence as redelivery
from tests.phase_a.test_execution_trace import commit_run
from tests.phase_a.test_recovery_coverage import evidence as recovery


class ComparisonBoundaryTests(unittest.TestCase):
    def test_unresolved_business_workflow_does_not_invent_an_escalation_event(self):
        from chaos.workflow_trace import outcome_class
        steps = [dict(state='SUCCEEDED', escalated_at=None), dict(state='FAILED_CONFIRMED', escalated_at=None),
                 dict(state='FAILED_CONFIRMED', escalated_at=None)]
        self.assertEqual(outcome_class('COMPENSATION_UNRESOLVED', steps), 'pending')
        steps[-1].update(state='ESCALATED_UNRESOLVED', escalated_at=123)
        self.assertEqual(outcome_class('COMPENSATION_UNRESOLVED', steps), 'escalated_unresolved')

    def test_owned_kill_request_is_visible_during_the_exit_before_wait_returns(self):
        from chaos.baselines.arm import B3Arm
        from chaos.baselines.stack import Stack
        stack = Stack.__new__(Stack)
        process = Mock(pid=222, returncode=-9)
        process.poll.side_effect = [None, -9]
        process.wait.return_value = -9
        stack.processes = {'worker': process}
        stack._expected_exits = {}
        stack._quiescing = False
        stack.events = []
        stack.errors = []
        stack.components = ('worker',)
        stack.controller = SimpleNamespace(errors=[], thread=SimpleNamespace(is_alive=lambda: True))
        arm = B3Arm()
        arm.stack = stack
        arm.errors = []
        arm._sample = Mock(return_value=SimpleNamespace(operations={}))
        with patch('chaos.baselines.stack.os.getpgid', return_value=222), \
                patch('chaos.baselines.stack.os.killpg', side_effect=lambda *_: arm.observe([])):
            stack.kill('worker')
        self.assertEqual(stack._expected_exits, {222: -9})
        self.assertEqual(arm.errors, [])

    def test_failed_owned_signal_cannot_hide_an_unproved_exit(self):
        from chaos.baselines.stack import Stack
        stack = Stack.__new__(Stack)
        process = Mock(pid=222)
        process.poll.return_value = None
        process.wait.return_value = -15
        stack.processes = {'worker': process}
        stack._expected_exits = {}
        stack.events = []
        stack.errors = []
        with patch('chaos.baselines.stack.os.getpgid', return_value=222), \
                patch('chaos.baselines.stack.os.killpg'), self.assertRaises(RuntimeError):
            stack.kill('worker')
        self.assertEqual(stack._expected_exits, {})
        self.assertEqual(stack.errors, ['requested_exit_unproved:worker'])

    def breaker(self):
        raw = recovery('outage')
        key = 'private-correlation'
        raw['database'].extend(breaker_rows([('op', 4, 'UNKNOWN', 'mock-payments', key,
            'provider-account', 5, 5, 9999, True)], 45, 46))
        return raw

    def test_breaker_requires_committed_open_account_after_real_failed_http(self):
        label = 'breaker_open_with_unresolved_work'
        self.assertIn(label, coverage.observed(self.breaker()))
        for mutation in ('account', 'closed', 'threshold', 'state', 'http', 'future', 'provider', 'timing'):
            raw = self.breaker()
            sample = raw['database'][-1]
            if mutation == 'account':
                sample['provider_account_id'] = None
            elif mutation == 'closed':
                sample['breaker_open'] = False
            elif mutation == 'threshold':
                sample['consecutive_failures'] = 4
            elif mutation == 'state':
                sample['state'] = 'SUCCEEDED'
            elif mutation == 'http':
                raw['http'].pop()
            elif mutation == 'provider':
                sample['provider'] = 'crm'
            elif mutation == 'future':
                sample.update(sample_started_ns=201, sample_finished_ns=202, monotonic_ns=202)
            else:
                sample.update(sample_started_ns=25, sample_finished_ns=26, monotonic_ns=26)
            with self.subTest(mutation=mutation):
                self.assertNotIn(label, coverage.observed(raw))

    def test_commit_stage_detail_requires_the_same_independently_visible_transaction(self):
        raw = commit_run()
        self.assertEqual(coverage.boundary_details(raw)['commit_ack_stages'], ['outcome'])
        raw['execution'][-1]['commit_xid'] = '456'
        self.assertEqual(coverage.boundary_details(raw)['commit_ack_stages'], [])

    def test_concurrent_refusal_requires_the_actual_duplicate_return_and_live_fence(self):
        raw = redelivery()
        raw['arm'] = 'b3'
        self.assertTrue(coverage.boundary_details(raw)['concurrent_reclaim_refused'])
        for mutation in ('return', 'lease', 'arm', 'identity'):
            altered = copy.deepcopy(raw)
            if mutation == 'return':
                altered['broker'][6]['task_status'] = 'recorded'
            elif mutation == 'lease':
                altered['database'][-1]['lease_active'] = False
            elif mutation == 'arm':
                altered['arm'] = 'b1'
            else:
                altered['database'][-1]['claim_id'] = 'changed'
            with self.subTest(mutation=mutation):
                self.assertFalse(coverage.boundary_details(altered)['concurrent_reclaim_refused'])


if __name__ == '__main__':
    unittest.main()
