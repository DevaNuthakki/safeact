"""B0 real subprocess acceptance, death, weak retries and memory-only workflows."""

import unittest

from chaos.baselines.naive_arm import B0Arm
from chaos.scenarios import OperationSpec,Scenario,generate
from tests.integration import test_baseline_arm as fixture


@unittest.skipUnless(fixture.ENABLED,'Owned local B0 HTTP/process development checks are opt-in')
class NaiveBaselineTests(unittest.TestCase):
    setUp=fixture.BaselineArmTests.setUp
    cleanup=fixture.BaselineArmTests.cleanup
    check=fixture.BaselineArmTests.check
    arm_class=B0Arm

    def case(self,family='CONTROL',faults=(),submits=1,concurrent=False,changed=False):
        return Scenario('DEV-B0-'+family,'dev',family,'payments','q-v2',7,
            (OperationSpec('b0-target',500,submits=submits,concurrent=concurrent,changed_resubmit=changed),),tuple(faults),True,10)

    def test_synchronous_control_has_no_database_or_broker_evidence(self):
        self.check('CONTROL',0,scenario=self.case())
        self.assertEqual(self.raw['database'],[]);self.assertEqual(self.raw['broker'],[])
        self.assertFalse(self.raw['infrastructure']['database_created'])
        self.assertTrue(self.raw['infrastructure']['acceptance'])
        self.assertTrue(all(not a['durable'] for a in self.raw['infrastructure']['acceptance']))

    def test_concurrent_copies_are_independent_unguarded_calls(self):
        self.check('F04',0,scenario=self.case('F04',submits=4,concurrent=True),expect_safe=False)
        self.assertEqual(self.result['operations'][0]['accepted_operations'],4)
        self.assertEqual(self.result['operations'][0]['effects'],4)

    def test_changed_request_is_not_given_an_invented_durable_conflict_guard(self):
        self.check('F10',0,scenario=self.case('F10',changed=True),expect_safe=False)
        self.assertEqual(self.result['changed_accepted'],1)

    def test_lost_reply_retries_with_a_fresh_native_key(self):
        faults=({'kind':'provider','provider':'payments','switch':'fail_after_effect','value':True},)
        self.check('F01',0,scenario=self.case('F01',faults=faults),expect_safe=False)
        self.assertEqual(self.result['operations'][0]['effects'],2)

    def test_killed_caller_retains_observed_acceptance_and_actual_effect(self):
        self.check('F05',0,('worker_crashed_after_send',),scenario=self.case('F05',faults=({'kind':'worker_kill','point':'after_provider_call'},)))
        self.assertEqual(self.result['operations'][0]['accepted_operations'],1)
        self.assertEqual(self.result['operations'][0]['effects'],1)
        self.assertEqual(self.result['unaccounted'],0)
        self.assertEqual(self.result['operations'][0]['outcome_class'],'pending')

    def test_pre_send_kill_never_invents_a_provider_effect(self):
        self.check('F05',0,('worker_crashed_before_send',),scenario=self.case('F05',faults=({'kind':'worker_kill','point':'after_fence_commit'},)))
        self.assertEqual(self.result['operations'][0]['effects'],0)
        self.assertEqual(self.result['unaccounted'],0)

    def test_fixed_workflow_control_has_actual_process_binding(self):
        self.check('F13',4)
        self.assertEqual(self.result['forward_effects'],2)
        self.assertEqual(self.raw['database'],[])
        self.assertTrue(self.raw['infrastructure']['process_snapshots'])

    def test_lost_refund_reply_retains_both_inverse_effects(self):
        self.check('F13',0,expect_safe=False)
        self.assertEqual(self.result['compensation_effects'],2)
        self.assertEqual(self.result['orphan_effects'],0)
        self.assertEqual(self.result['invalid_compensation_effects'],0)
