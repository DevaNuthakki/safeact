import unittest

from chaos.oracle import _observed_ledger, _provider_metrics
from chaos.scoring import aggregate, summarize


def event(name, at, *, method, resource="charge-1", error_type=None):
    return {
        "source": "provider",
        "event": name,
        "method": method,
        "resource": resource,
        "monotonic_ns": at,
        "error_type": error_type,
    }


class ProviderTraceMetricsTests(unittest.TestCase):
    def test_cleanup_calls_after_observation_boundary_do_not_inflate_metrics(self):
        before = event("provider_start", 10, method="refund")
        cleanup = event("provider_start", 21, method="lookup")

        observed = _observed_ledger({
            "observation_finished_monotonic_ns": 20,
            "ledger": [before, cleanup],
        })

        self.assertEqual(observed, [before])
        self.assertEqual(_provider_metrics(observed)["charge-1"]["provider_calls"], 1)

    def test_verification_delay_uses_first_ambiguous_failure_and_completed_lookup(self):
        rows = [
            event("provider_start", 1_000_000_000, method="refund"),
            event("provider_error", 2_000_000_000, method="refund",
                  error_type="ProviderTimeout"),
            event("provider_start", 4_000_000_000, method="lookup"),
            event("provider_end", 5_000_000_000, method="lookup"),
            event("provider_start", 6_000_000_000, method="refund"),
            event("provider_error", 7_000_000_000, method="refund",
                  error_type="ProviderTimeout"),
            event("provider_start", 8_000_000_000, method="lookup"),
            event("provider_end", 9_000_000_000, method="lookup"),
        ]

        measured = _provider_metrics(rows)["charge-1"]

        self.assertEqual(measured["provider_dispatch_calls"], 2)
        self.assertEqual(measured["provider_lookup_calls"], 2)
        self.assertEqual(measured["provider_calls"], 4)
        self.assertEqual(measured["verification_delay_seconds"], 3.0)
        self.assertEqual(measured["provider_call_cost_usd"], 0.0)
        self.assertEqual(measured["provider_cost_basis"],
                         "mock_only_zero_external_cost")

    def test_definitive_rejection_and_incomplete_lookup_do_not_count_as_verification(self):
        rows = [
            event("provider_start", 1, method="refund"),
            event("provider_error", 2, method="refund",
                  error_type="ProviderRejected"),
            event("provider_start", 3, method="lookup"),
            event("provider_error", 4, method="lookup",
                  error_type="LookupUnavailable"),
        ]

        self.assertIsNone(_provider_metrics(rows)["charge-1"]
                          ["verification_delay_seconds"])

    def test_aggregate_exposes_verification_distribution_and_mock_cost(self):
        row = {
            "scenario_id": "D-F01-0000",
            "arm": "safeact",
            "family": "F01",
            "connector": "payments",
            "completion_achievable": True,
            "expected_operations": 1,
            "operations": [{
                "index": 0,
                "accepted_operations": 1,
                "effects": 1,
                "outcome_class": "confirmed_success",
                "safe_completion": True,
                "provider_dispatch_calls": 1,
                "provider_lookup_calls": 1,
                "provider_calls": 2,
                "provider_call_cost_usd": 0.0,
                "provider_cost_basis": "mock_only_zero_external_cost",
                "verification_delay_seconds": 0.75,
            }],
        }

        result = aggregate([row])
        summary = summarize([row])

        self.assertEqual(result["provider_calls"], 2)
        self.assertEqual(result["provider_call_cost_usd"], 0.0)
        self.assertEqual(
            result["latency_seconds"]["verification_delay_seconds"]["p50"],
            0.75,
        )
        self.assertIn("verification_delay_seconds",
                      summary["by_arm"]["safeact"]["latency_seconds"])
        self.assertIn("no external charge", summary["provider_cost_method"])


if __name__ == "__main__":
    unittest.main()
