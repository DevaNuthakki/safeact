"""Unregistered B3 adapter through actual slot proxies and the common runner."""

import os
import json
import tempfile
import unittest
from pathlib import Path

from chaos import coverage,oracle
from chaos.baselines.arm import B3Arm
from chaos.runner import run
from chaos.scenarios import generate
from chaos.slot import Slot
from chaos.quiescence import process_group_exists
from deploy.evaluation.agent_v1.secret_scan import scan_text


ENABLED=all(os.environ.get(k)=='1' for k in ('SAFEACT_RUN_DB_INTEGRATION','SAFEACT_RUN_DISPOSABLE_DB_TESTS','SAFEACT_RUN_BROKER_TESTS'))


@unittest.skipUnless(ENABLED,'Actual B3 common-runner development checks require local database/broker/Toxiproxy')
class BaselineArmTests(unittest.TestCase):
    arm_class=B3Arm
    def setUp(self):
        self.private=tempfile.TemporaryDirectory(prefix='safeact-b3-arm-');self.addCleanup(self.private.cleanup)
        self.slot=None;self.arm=None;self.raw={};self.addCleanup(self.cleanup)

    def cleanup(self):
        errors=[]
        for owner in (self.arm,self.slot):
            if owner is not None:
                try:owner.teardown() if owner is self.arm else owner.stop()
                except Exception as error:errors.append(error)
        if self.arm is not None:
            self.raw['adapter_cleanup']=dict(database_resources_removed=not hasattr(self.arm,'env') or not self.arm.env.created,
                private_config_removed=not self.arm.stack.config_path.exists(),
                processes_stopped=all(g['process'].poll() is not None and not process_group_exists(g['process'].pid) for g in self.arm.stack.generations),
                controller_stopped=(not self.arm.stack.controller.thread.is_alive()) if hasattr(self.arm.stack,'controller') else
                    all(not g['thread'].is_alive() for g in self.arm.stack.generations))
            capture=os.environ.get('SAFEACT_BASELINE_ARM_CAPTURE_DIR')
            if capture:
                text=json.dumps(self.raw,indent=2,default=str)+'\n'
                if (hasattr(self.arm,'env') and self.arm.env.password in text) or scan_text(text,suffix='.json'):raise RuntimeError('Private data in baseline observations')
                folder=Path(capture);folder.mkdir(parents=True,exist_ok=True)
                (folder/(self._testMethodName+'.json')).write_text(text)
            self.assertTrue(all(self.raw['adapter_cleanup'].values()))
        if errors:raise errors[0]

    def check(self,family,index,labels=(),*,scenario=None,expect_safe=True):
        scenario=scenario or generate('dev',family,index,7)
        self.slot=Slot(6,isolated_broker=any(f['kind']=='broker_kill' for f in scenario.faults));self.slot.start()
        self.arm=self.arm_class();self.arm.setup(self.slot,Path(self.private.name)/'arm')
        self.raw=dict(run(self.slot,self.arm,scenario),scenario=json.loads(scenario.to_json()))
        if self.slot.private_broker is not None:self.raw['isolated_broker']=list(self.slot.private_broker.events)
        self.result=oracle.evaluate(scenario,self.raw)
        self.assertEqual(self.result['harness_errors'],[],self.result['harness_errors'])
        self.assertEqual(oracle.safety_failures(self.result),not expect_safe,self.result)
        self.assertTrue(self.raw['quiescence']['complete'])
        self.assertTrue(set(labels)<=coverage.observed(self.raw),coverage.observed(self.raw))

    def test_control(self):self.check('CONTROL',0)
    def test_duplicate_submissions(self):self.check('F04',0,('duplicate_submissions_collapsed',))
    def test_changed_submission(self):self.check('F10',0,('changed_payload_refused',))
    def test_lost_commit_ack(self):self.check('F11',0,('commit_ack_lost',))
    def test_worker_kill(self):
        scenario=generate('dev','F05',0,7);point=scenario.faults[0]['point']
        self.check('F05',0,('worker_crashed_after_send' if point in ('after_provider_call','before_outcome_commit') else 'worker_crashed_before_send',))
    def test_expired_lease_during_request(self):self.check('F06',0,('lease_expired_while_request_ran',))
    def test_late_commit_after_empty_lookup(self):self.check('F07',0,('late_commit_after_empty_lookup',))
    def test_queued_broker_loss(self):self.check('F08',0,('broker_messages_lost',))
    def test_early_broker_redelivery(self):self.check('F08',8,('broker_redelivered_during_dispatch',))
    def test_actual_broker_kill(self):self.check('F08',12,('broker_messages_lost','broker_process_killed'))
    def test_workflow_compensation(self):self.check('F13',0,('compensation_applied_response_lost','compensation_verified_by_lookup'))
    def test_review_concurrent(self):self.check('F14',0,('reviewers_serialized_on_binding',))
    def test_review_stale(self):self.check('F14',1,('stale_review_refused_after_evidence',))
    def test_review_protected_retry(self):self.check('F14',2,('reviewed_retry_after_proven_nonapplication',))
