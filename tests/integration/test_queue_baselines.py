"""Actual B1/B2 differences, without requiring weaker arms to behave like B3."""

import unittest

from chaos.baselines.queue_arm import B1Arm,B2Arm
from chaos.scenarios import OperationSpec,Scenario
from tests.integration import test_baseline_arm as fixture


@unittest.skipUnless(fixture.ENABLED,'Disposable queue-baseline process checks are opt-in')
class QueueBaselineTests(unittest.TestCase):
    setUp=fixture.BaselineArmTests.setUp
    cleanup=fixture.BaselineArmTests.cleanup
    check=fixture.BaselineArmTests.check
    arm_class=B1Arm

    def lost_reply(self,connector='payments'):
        return Scenario('DEV-QUEUE-LOST-'+connector,'dev','F01',connector,'q-v2',7,
            (OperationSpec('queue-target-'+connector,500),),
            ({'kind':'provider','provider':connector,'switch':'fail_after_effect','value':True},),True,12)

    def test_b1_fresh_native_keys_repeat_an_applied_request(self):
        self.check('F01',0,scenario=self.lost_reply(),expect_safe=False)
        self.assertEqual(self.result['operations'][0]['effects'],2)
        keys={e['correlation_sha256'] for e in self.raw['ledger'] if e['event']=='call'}
        self.assertEqual(len(keys),2)

    def test_b2_stable_native_key_can_match_safe_execution(self):
        self.arm_class=B2Arm;self.check('F01',0,scenario=self.lost_reply())
        self.assertEqual(self.result['operations'][0]['effects'],1)
        self.assertEqual(len({e['correlation_sha256'] for e in self.raw['ledger'] if e['event']=='call'}),1)

    def test_b1_non_native_retries_can_duplicate(self):
        self.check('F01',0,scenario=self.lost_reply('crm'),expect_safe=False)

    def test_b2_does_not_invent_native_support_for_crm(self):
        self.arm_class=B2Arm;self.check('F01',0,scenario=self.lost_reply('crm'),expect_safe=False)

    def test_b1_early_redelivery_executes_the_duplicate(self):
        self.check('F08',8,('broker_redelivered_during_dispatch',),expect_safe=False)

    def test_b2_early_redelivery_preserves_native_protection(self):
        self.arm_class=B2Arm;self.check('F08',8,('broker_redelivered_during_dispatch',))

    def test_b1_collapses_identical_durable_submissions(self):self.check('F04',0,('duplicate_submissions_collapsed',))
    def test_b1_refuses_changed_durable_intent(self):self.check('F10',0,('changed_payload_refused',))
    def test_b1_recovers_a_queued_broker_loss(self):self.check('F08',0,('broker_messages_lost',))
    def test_b2_records_actual_lost_commit_ack(self):
        self.arm_class=B2Arm;self.check('F11',0,('commit_ack_lost',))

    def test_b1_inverse_retries_remain_attributed_to_the_inverse(self):
        self.check('F13',0,expect_safe=False)
        self.assertEqual(self.result['compensation_effects'],2)
        self.assertEqual(self.result['orphan_effects'],0)
        self.assertEqual(self.result['invalid_compensation_effects'],0)

    def test_b2_inverse_replay_can_complete_without_a_lookup(self):
        self.arm_class=B2Arm;self.check('F13',0)
        self.assertEqual(self.result['compensation_effects'],1)

    def test_neither_queue_mode_reads_the_provider_lookup_surface(self):
        self.arm_class=B2Arm;self.check('F01',0,scenario=self.lost_reply())
        self.assertFalse(any(e['event']=='http_start' and e['method']=='GET' for e in self.raw['http']))
