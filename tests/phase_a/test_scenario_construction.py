"""Source-only construction audit. Held-out scenarios are never executed here."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chaos.applicability import critical_labels, reason
from chaos.baselines.arm import B3Arm
from chaos.baselines.naive_arm import B0Arm
from chaos.baselines.processes import Hook
from chaos.baselines.queue_arm import B1Arm, B2Arm
from chaos.fault_contracts import validate_fault, validate_scenario
from chaos.profiles import profile
from chaos.runner import FaultDriver, Timeline
from chaos.scenarios import COMBINATIONS, FAMILIES, Scenario, _storm_achievable, generate
from chaos.workflow_faults import WorkflowPayments
from connectors.ledger import EffectsLedger
from connectors.payments_mock import LookupUnavailable, ProviderTimeout
from core.contracts import LookupSemantics


class ScenarioConstructionTests(unittest.TestCase):
    def test_all_family_and_combination_definitions_use_supported_controls(self):
        for family in FAMILIES + COMBINATIONS:
            for index in range(100):
                case = generate('definition-audit', family, index, 7193)
                with self.subTest(family=family, index=index):
                    validate_scenario(case)
                    self.assertEqual(Scenario.from_json(case.to_json()), case)

    def test_unsupported_provider_switch_is_rejected_before_mutation(self):
        mock = SimpleNamespace()
        driver = FaultDriver(SimpleNamespace(mocks={'payments': mock}, proxies={}), Timeline())
        with self.assertRaises(ValueError):
            driver.apply(dict(kind='provider', provider='payments', switch='corrupt_search', value=True))
        self.assertFalse(hasattr(mock, 'corrupt_search'))
        for fault in (dict(kind='reveal', provider='email'), dict(kind='later', provider='unknown', switch='outage'),
                      dict(kind='workflow_lookup', provider='crm', step='compensation', switch='search_unavailable', value=True)):
            with self.assertRaises(ValueError):
                validate_fault(fault)

    def test_inverse_combination_controls_never_target_the_initial_charge(self):
        for index in range(100):
            case = generate('definition-audit', 'F13+F12', index, 31)
            self.assertEqual([f['kind'] for f in case.faults], ['workflow', 'workflow_lookup'])
            self.assertIn(case.faults[0]['mode'], ('lost_response', 'hidden_response'))
            self.assertEqual(case.faults[1]['step'], 'compensation')
            self.assertFalse(any(f.get('switch') == 'corrupt_search' for f in case.faults))
            for arm in (B0Arm, B1Arm, B2Arm):
                self.assertIsNotNone(reason(arm, case))

    def test_inverse_lookup_control_activates_only_at_refund_entry(self):
        provider = WorkflowPayments(ledger=EffectsLedger())
        provider.configure_workflow('lost_response', lambda: None)
        provider.configure_inverse_lookup_outage()
        receipt = provider.charge('charge', {'customer_id': 'customer', 'amount_minor': 100})
        self.assertEqual(provider.lookup('charge').status, 'found')
        with self.assertRaises(ProviderTimeout):
            provider.refund('inverse', {'charge_id': receipt.refund_id, 'amount_minor': 100})
        with self.assertRaises(LookupUnavailable):
            provider.lookup('inverse')

    def test_combined_lease_and_empty_lookup_use_one_held_request_and_eventual_contract(self):
        for index in range(100):
            case = generate('definition-audit', 'F06+F07', index, 31)
            held = [f for f in case.faults if f['kind'] == 'hold_after_empty_lookup']
            self.assertEqual(len(held), 1)
            self.assertFalse(held[0]['respond_now'])
            self.assertGreater(held[0]['release_ms'], 3000)
            contract = profile('mock-'+case.connector, case.contract_version)
            self.assertEqual(contract.request_deadline.total_seconds(), 6)
            self.assertEqual(contract.contract.lookup_semantics, LookupSemantics.EVENTUAL)
            self.assertFalse(any(f['kind'] == 'hold' for f in case.faults))

    def test_eventual_inflight_profile_preserves_native_keys_while_its_index_lags(self):
        provider = WorkflowPayments(ledger=EffectsLedger())
        provider.register_key('unit-key', 'q-v2-eventual-inflight')
        provider.hide_new_effects = True
        params = dict(charge_id='original-charge', amount_minor=100)
        first = provider.refund('unit-key', params)
        replay = provider.refund('unit-key', params)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.refund_id, first.refund_id)
        self.assertEqual(provider.lookup('unit-key').status, 'absent')
        provider.reveal_hidden_effects()
        self.assertEqual(provider.lookup('unit-key').status, 'found')

    def test_combined_broker_and_worker_loss_has_exactly_one_owned_kill_watcher(self):
        for index in range(100):
            case = generate('definition-audit', 'F05+F08', index, 31)
            self.assertEqual(sum(f['kind'] == 'worker_kill' for f in case.faults), 1)
            self.assertEqual(sum(f['kind'] == 'broker_loss' for f in case.faults), 1)

    def test_global_rejection_budget_cannot_exceed_the_spare_attempt_capacity(self):
        self.assertTrue(_storm_achievable('payments', 4, [dict(kind='rate_limit', count=8)]))
        self.assertFalse(_storm_achievable('payments', 4, [dict(kind='rate_limit', count=9)]))
        self.assertFalse(_storm_achievable('crm', 4, [dict(kind='rate_limit', count=4)]))
        self.assertFalse(_storm_achievable('crm', 4, [dict(kind='rate_limit', count=5)]))

    def test_long_outages_cannot_borrow_time_from_beyond_the_investigation_window(self):
        for connector in ('payments', 'crm'):
            self.assertFalse(_storm_achievable(connector, 1, [dict(kind='outage', duration_ms=12000)]))
        self.assertTrue(_storm_achievable('payments', 1, [dict(kind='outage', duration_ms=5992)]))
        self.assertFalse(_storm_achievable('payments', 1, [dict(kind='outage', duration_ms=8000)]))

    def test_weak_guards_stay_in_coverage_requirements_while_absent_surfaces_do_not(self):
        for arm in (B0Arm, B1Arm, B2Arm, B3Arm):
            required = critical_labels(arm)
            self.assertIn('duplicate_submissions_collapsed', required)
            self.assertIn('changed_payload_refused', required)
            self.assertNotIn('agent_self_approval_refused', required)
        self.assertIn('reviewers_serialized_on_binding', critical_labels(B3Arm))
        self.assertNotIn('reviewers_serialized_on_binding', critical_labels(B2Arm))
        self.assertNotIn('broker_messages_lost', critical_labels(B0Arm))

    def test_park_marker_is_invisible_until_the_complete_json_can_be_read(self):
        import json
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            (root/'before_provider_call').write_text('park')
            (root/'before_provider_call').chmod(0o600)
            original = json.dumps
            def serialize(value, **kwargs):
                if 'pid' in value and 'event' not in value:
                    self.assertFalse((root/'parked').exists())
                return original(value, **kwargs)
            class Parked(Exception):
                pass
            with patch('chaos.baselines.processes.json.dumps', side_effect=serialize), \
                    patch('chaos.baselines.processes.threading.Event') as event:
                event.return_value.wait.side_effect = Parked
                with self.assertRaises(Parked):
                    Hook(root)('before_provider_call', {})
                event.return_value.wait.assert_called_once()
            self.assertEqual(json.loads((root/'parked').read_text())['point'], 'before_provider_call')
            self.assertEqual(list(root.glob('.parked-*')), [])


if __name__ == '__main__':
    unittest.main()
