"""Demo/E2E contracts keep the real contract shapes; faults are harness-only."""

import unittest

from core.connector_profiles import MOCK_PROFILES
from deploy.demo.profiles import DEMO_PROFILES, E2E_PROFILES
from deploy.demo.run_stack import scripted

# Everything except the windows (and the labelled reference) must match.
TIMING = {
    "request_deadline_seconds", "investigation_window_seconds",
    "post_escalation_check_interval_seconds",
    "post_escalation_horizon_seconds", "key_safety_margin_seconds",
    "contract_reference",
}


class ScaledProfileTests(unittest.TestCase):

    def test_same_contracts_and_capabilities_only_faster(self):
        real = {(p.connector_id, p.version): p.row() for p in MOCK_PROFILES}
        for profiles in (DEMO_PROFILES, E2E_PROFILES):
            scaled = {(p.connector_id, p.version): p.row() for p in profiles}
            self.assertEqual(set(scaled), set(real))
            for key, row in scaled.items():
                with self.subTest(contract=key):
                    for column, value in row.items():
                        if column not in TIMING:
                            self.assertEqual(value, real[key][column], column)
                    self.assertIn("scaled timings", row["contract_reference"])


class FaultScriptTests(unittest.TestCase):

    def test_only_the_named_synthetic_resources_misbehave(self):
        self.assertEqual(
            scripted("payments", {"charge_id": "ch_north_1002"}),
            "timeout_after_success",
        )
        self.assertEqual(
            scripted("crm", {"case_id": "case_south_2003"}), "rejected_retryable"
        )
        self.assertEqual(
            scripted("email", {"customer_id": "cust_north_3002"}),
            "sent_response_lost",
        )
        for kind, params in (
            ("payments", {"charge_id": "ch_north_1001"}),
            ("crm", {"case_id": "ch_north_1002"}),
            ("payments", {"charge_id": "ch_north_10021"}),
            ("email", {"customer_id": "cust_north_3001"}),
        ):
            with self.subTest(kind=kind, params=params):
                self.assertIsNone(scripted(kind, params))

    def test_the_api_has_no_way_to_name_a_fault(self):
        import api.actions
        import api.catalog
        for module in (api.actions, api.catalog):
            with open(module.__file__, encoding="utf-8") as stream:
                source = stream.read()
            self.assertNotIn("run_stack", source)
            self.assertNotIn("scripted", source)

    def test_permanent_order_rejection_has_no_effect_and_leaves_other_cases_clean(self):
        from connectors.crm_mock import CrmValidationRejected
        from connectors.ledger import EffectsLedger
        from deploy.demo.run_stack import ScriptedCrm

        ledger = EffectsLedger()
        provider = ScriptedCrm(ledger=ledger)
        with self.assertRaises(CrmValidationRejected) as rejected:
            provider.create_record("rejected-order", {"case_id": "case_north_2004", "sku": "demo"})
        self.assertFalse(rejected.exception.retryable)
        self.assertEqual(ledger.effects_for("rejected-order"), ())
        provider.create_record("clean-order", {"case_id": "case_north_2001", "sku": "demo"})
        self.assertEqual(len(ledger.effects_for("clean-order")), 1)


if __name__ == "__main__":
    unittest.main()
