"""Lease coverage needs database answers inside an actual provider request."""

import copy
import json
import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chaos import coverage, qualify
from chaos.arms.base import Admission, OperationView
from chaos.database_trace import audit_rows, lease_rows
from chaos.profiles import LEASE_SECONDS, profile
from chaos.runner import run
from chaos.scenarios import Scenario, generate
from connectors.http_trace import correlation_hash
from connectors.ledger import EffectsLedger


def evidence():
    key = correlation_hash("provider-key")
    def event(kind, at, **details):
        return dict(event=kind, source="http_client" if kind.startswith("http_") else "provider",
            monotonic_ns=at, provider="payments", correlation_sha256=key, **details)
    samples = []
    for started, finished, active in ((30, 40, True), (60, 70, False)):
        samples += lease_rows([("local", 2, "claim", "attempt", 1000, active,
                                "mock-payments", "provider-key")], started, finished)
    return dict(admissions=[dict(index=0, local_id="local", changed=False)],
        database=samples, http=[event("http_start", 10, request_id="client", method="POST"),
            event("http_response", 100, request_id="client", method="POST", status_code=200)],
        ledger=[event("provider_start", 20, provider_call_id="provider", method="refund"),
            event("provider_end", 90, provider_call_id="provider", method="refund")],
        observation_finished_monotonic_ns=110)


def credited(raw):
    return "lease_expired_while_request_ran" in coverage.observed(raw)


class LeaseEvidenceTests(unittest.TestCase):
    def test_live_then_expired_persisted_lease_inside_request_is_observed(self):
        self.assertTrue(credited(evidence()))

    def test_a_lease_and_long_http_interval_without_provider_request_are_insufficient(self):
        raw = evidence()
        raw["ledger"] = []
        self.assertFalse(credited(raw))

    def test_sleep_before_sending_does_not_establish_lease_expiry_in_flight(self):
        raw = evidence()
        raw["ledger"][0]["monotonic_ns"] = 80
        self.assertFalse(credited(raw))

    def test_lease_samples_after_request_end_do_not_count(self):
        raw = evidence()
        raw["ledger"][1]["monotonic_ns"] = 50
        self.assertFalse(credited(raw))

    def test_client_timeout_before_expiry_does_not_prove_overlapping_client_request(self):
        raw = evidence()
        raw["http"][1]["monotonic_ns"] = 50
        self.assertFalse(credited(raw))

    def test_unfinished_provider_or_client_interval_is_not_credited(self):
        for key in ("ledger", "http"):
            raw = evidence()
            raw[key].pop()
            self.assertFalse(credited(raw))

    def test_unrelated_operation_provider_or_key_cannot_supply_lease_evidence(self):
        for key, value in (("operation_id", "other"), ("provider", "crm"),
                           ("correlation_sha256", correlation_hash("other"))):
            raw = evidence()
            for row in raw["database"]:
                row[key] = value
            self.assertFalse(credited(raw))

    def test_before_and_after_must_describe_the_same_persisted_lease(self):
        for key, value in (("attempt_id", "later"), ("claim_id", "later"),
                           ("transition_seq", 4), ("lease_expires_at", 2000)):
            raw = evidence()
            raw["database"][1][key] = value
            self.assertFalse(credited(raw))

    def test_two_live_or_two_expired_samples_are_not_an_expiry_crossing(self):
        for active in (True, False):
            raw = evidence()
            for row in raw["database"]:
                row["lease_active"] = active
            self.assertFalse(credited(raw))

    def test_reversed_or_overlapping_query_intervals_are_not_a_crossing(self):
        for change in (dict(sample_started_ns=35), dict(sample_started_ns=80, sample_finished_ns=70)):
            raw = evidence()
            raw["database"][1].update(change)
            self.assertFalse(credited(raw))

    def test_ambiguous_http_attribution_does_not_receive_credit(self):
        raw = evidence()
        raw["http"] += [dict(row, request_id="second") for row in copy.deepcopy(raw["http"])]
        self.assertFalse(credited(raw))

    def test_cleanup_only_expiry_cannot_establish_timed_fault_coverage(self):
        raw = evidence()
        raw["observation_finished_monotonic_ns"] = 50
        self.assertFalse(credited(raw))


class DatabaseTraceTests(unittest.TestCase):
    def test_records_hash_correlation_and_preserve_actual_lease(self):
        row = lease_rows([("op", 2, "claim", "attempt", 1234.5, True,
                          "mock-payments", "private-key")], 10, 20)[0]
        self.assertEqual(row["correlation_sha256"], correlation_hash("private-key"))
        self.assertEqual(row["lease_expires_at"], 1234.5)
        self.assertNotIn("private-key", json.dumps(row))

    def test_invalid_query_bounds_and_incomplete_leases_fail(self):
        row = ("op", 2, "claim", "attempt", 1234, True, "mock-payments", "key")
        for start, finish in ((20, 10), (0, 10), (True, 10)):
            with self.assertRaises(ValueError):
                lease_rows([row], start, finish)
        with self.assertRaises(ValueError):
            lease_rows([tuple(None if i == 3 else value for i, value in enumerate(row))], 10, 20)

    def test_committed_xids_are_retained_without_claiming_lost_ack_coverage(self):
        rows = audit_rows([(1, "op", "transition", "123456", 1000)], 10, 20)
        self.assertEqual(rows[0]["commit_xid"], "123456")
        self.assertNotIn("commit_ack_lost", coverage.observed(dict(database=rows)))
        with self.assertRaises(ValueError):
            audit_rows([(1, "op", "transition", None, 1000)], 10, 20)

    def test_database_observation_failure_is_retained_as_a_harness_error(self):
        class Arm:
            name, database_trace_enabled = "safeact", True
            def prepare(self, case):
                pass
            def submit(self, *args, **kwargs):
                return Admission("local")
            def observe(self, ids):
                return {i: OperationView(i, "confirmed_success", "SUCCEEDED") for i in ids}
            def quiesce(self):
                pass
            def database_observations(self, ids):
                raise RuntimeError("database trace unavailable")
            def check_integrity(self):
                return []
        with tempfile.TemporaryDirectory() as directory:
            slot = SimpleNamespace(index=0, ledger=EffectsLedger(), reset=lambda: None,
                fault_dir=Path(directory), close_provider_admission=lambda: None,
                wait_provider_idle=lambda timeout: None)
            with patch("chaos.runner.STABLE_SECONDS", 0):
                raw = run(slot, Arm(), generate("dev", "CONTROL", 0, 7))
        self.assertTrue(any("database trace unavailable" in e for e in raw["harness_errors"]))

    def test_f06_holds_provider_past_lease_within_the_request_deadline(self):
        for index in range(12):
            case = generate("dev", "F06", index, 7)
            fault = case.faults[0]
            self.assertEqual(fault["kind"], "hold")
            self.assertFalse(fault["respond_now"])
            self.assertGreater(fault["release_ms"] / 1000, LEASE_SECONDS)
            contract = profile("mock-" + case.connector, case.contract_version)
            self.assertLess(fault["release_ms"] / 1000, contract.request_deadline.total_seconds())
            self.assertEqual(case, Scenario.from_json(case.to_json()))

    def test_development_f06_includes_every_connector_contract(self):
        cases = qualify.scenarios_for(SimpleNamespace(corpus="dev", families="F06", limit=None))
        self.assertEqual({case.connector for case in cases}, {"payments", "crm", "email"})
        self.assertEqual(len(cases), 5)

    def test_f07_restores_lookup_within_the_window_for_both_connectors(self):
        cases = [generate("dev", "F07", index, 7) for index in range(3)]
        self.assertEqual({case.connector for case in cases}, {"payments", "crm"})
        for case in cases:
            self.assertTrue(case.completion_achievable)
            self.assertLess(max(f.get("at_ms", 0) for f in case.faults) / 1000, case.settle_seconds)


if __name__ == "__main__":
    unittest.main()
