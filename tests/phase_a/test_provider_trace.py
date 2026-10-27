"""Independent trace evidence and counterexamples; no external services."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chaos.coverage import observed
from chaos.provider_trace import ObservedProvider
from chaos.arms.base import Admission, OperationView
from chaos.runner import run
from chaos.scenarios import OperationSpec, Scenario
from connectors.http_trace import ObservedClient, TRACE_ERROR, correlation_hash, payload_hash
from connectors.ledger import EffectsLedger
from deploy.stack.http_trace import HttpTrace, from_environment, read_trace


KEY = correlation_hash("test-provider-key")
SCHEMA = "safeact_it_" + "a" * 32


def event(label, at, **details):
    return dict(event=label, monotonic_ns=at, at=1000 - at,
                source="http_client" if label.startswith("http_") else "provider",
                provider="payments", method="POST" if label.startswith("http_") else "refund",
                correlation_sha256=KEY, request_id="r1", provider_call_id="p1", **details)


def applied_run(end="http_error", **answer):
    return {"admissions": [], "http": [event("http_start", 10), event(end, 50, **answer)],
            "ledger": [event("provider_start", 20), event("call", 25,
                       payload_sha256=payload_hash({"amount": 10})),
                       event("effect", 30, effect_id="re_one"), event("provider_end", 40)]}


class ObservationTests(unittest.TestCase):
    def test_missing_or_corrupt_http_trace_fails_the_run(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as directory:
                ledger = EffectsLedger()
                class Arm:
                    name, http_trace_enabled = "safeact", True
                    def prepare(self, case):
                        pass
                    def submit(self, *args, **kwargs):
                        ledger.record_observation("provider_start", provider="payments",
                            method="refund", correlation_sha256=KEY)
                        ledger.record_call("test-provider-key", {"charge_id": "ch_test"})
                        ledger.record_effect("test-provider-key", "re_one")
                        return Admission("local")
                    def observe(self, ids):
                        return {i: OperationView(i, "confirmed_success", "SUCCEEDED") for i in ids}
                    def check_integrity(self):
                        return []
                    def quiesce(self):
                        pass
                    def trace_observations(self, since_ns):
                        if corrupt:
                            raise RuntimeError("corrupt HTTP trace")
                        return []
                case = Scenario("D-CONTROL-0000", "dev", "CONTROL", "payments", "q-v2",
                                7, (OperationSpec("ch_test", 100),), (), True, 1)
                slot = SimpleNamespace(index=0, ledger=ledger, fault_dir=Path(directory), reset=lambda: None,
                    close_provider_admission=lambda: None, wait_provider_idle=lambda timeout: None)
                with patch("chaos.runner.STABLE_SECONDS", 0):
                    raw = run(slot, Arm(), case)
                self.assertTrue(raw["harness_errors"])

    def test_client_preserves_response_and_does_not_record_raw_keys(self):
        rows, response, kwargs_seen = [], SimpleNamespace(status_code=200), []
        class Client:
            def post(self, path, **kwargs):
                kwargs_seen.append((path, kwargs))
                return response
        client = ObservedClient("payments", Client(), lambda event, **kw: rows.append(dict(event=event, **kw)))
        kwargs = dict(json={"amount": 10}, headers={"Idempotency-Key": "private-test-key"}, timeout=1)
        self.assertIs(client.post("/v1/refunds", **kwargs), response)
        self.assertEqual(kwargs_seen, [("/v1/refunds", kwargs)])
        self.assertEqual([r["event"] for r in rows], ["http_start", "http_response"])
        self.assertNotIn("private-test-key", json.dumps(rows))

    def test_client_preserves_the_actual_exception(self):
        failure, rows = TimeoutError("private exception message"), []
        class Client:
            def get(self, *args, **kwargs):
                raise failure
        client = ObservedClient("payments", Client(), lambda event, **kw: rows.append(dict(event=event, **kw)))
        with self.assertRaises(TimeoutError) as caught:
            client.get("/v1/lookup", params={"idempotency_key": "key"})
        self.assertIs(caught.exception, failure)
        self.assertEqual(rows[-1]["error_type"], "TimeoutError")
        self.assertNotIn("private exception message", json.dumps(rows))

    def test_observer_failure_marks_the_harness_without_changing_response(self):
        response = SimpleNamespace(status_code=200)
        client = SimpleNamespace(post=lambda *a, **kw: response)
        def broken(*args, **kwargs):
            raise OSError("trace device unavailable")
        with patch("connectors.http_trace.os.write") as marker:
            answer = ObservedClient("email", client, broken).post(
                "/messages", json={"message_id": "message"})
        self.assertIs(answer, response)
        self.assertEqual(marker.call_count, 2)
        marker.assert_called_with(2, TRACE_ERROR)

    def test_provider_observation_preserves_results_errors_and_fault_controls(self):
        ledger, receipt = EffectsLedger(), SimpleNamespace(status="absent")
        class Provider:
            outage = False
            def lookup(self, key):
                if self.outage:
                    raise ConnectionError("outage")
                return receipt
        provider = ObservedProvider("payments", Provider(), ledger.record_observation)
        self.assertIs(provider.lookup("key"), receipt)
        provider.outage = True
        with self.assertRaises(ConnectionError):
            provider.lookup("key")
        self.assertEqual(ledger.snapshot(), ((), ()))
        self.assertEqual([e["event"] for e in ledger.events()],
                         ["provider_start", "provider_end", "provider_start", "provider_error"])

    def test_ledger_events_are_copies_and_keep_effect_identity(self):
        ledger = EffectsLedger()
        ledger.record_call("key", {"amount": 10})
        ledger.record_effect("key", "re_one")
        events = ledger.events()
        self.assertEqual(events[1]["effect_id"], "re_one")
        events[1]["effect_id"] = "forged"
        self.assertEqual(ledger.events()[1]["effect_id"], "re_one")
        self.assertEqual([e["sequence"] for e in events], [0, 1])
        with self.assertRaises(ValueError):
            ledger.record_observation("effect", effect_id="forged")

    def test_trace_is_private_and_rejects_bad_schema_and_corrupt_records(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = HttpTrace(directory, SCHEMA)
            writer("http_start", provider="payments", method="POST", path="/v1/refunds",
                   request_id="b" * 32, correlation_sha256=KEY)
            rows = read_trace(directory, 0)
            self.assertEqual(len(rows), 1)
            path = next(Path(directory).glob("http-*.jsonl"))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(read_trace(directory, rows[0]["monotonic_ns"] + 1), [])
            with self.assertRaises(RuntimeError):
                HttpTrace(directory, "safeact_demo")
            path.write_text("{broken\n")
            with self.assertRaises(RuntimeError):
                read_trace(directory, 0)

    def test_trace_is_off_without_explicit_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(from_environment("safeact_demo"))

    def test_multiple_processes_append_complete_trace_records(self):
        with tempfile.TemporaryDirectory() as directory:
            code = ("import sys,uuid; from deploy.stack.http_trace import HttpTrace; "
                    "from connectors.http_trace import correlation_hash; "
                    "w=HttpTrace(sys.argv[1],sys.argv[2]); "
                    "[w('http_start',provider='payments',method='POST',path='/v1/refunds',"
                    "request_id=uuid.uuid4().hex,correlation_sha256=correlation_hash('key')) "
                    "for i in range(25)]")
            children = [subprocess.Popen([sys.executable, "-c", code, directory, SCHEMA],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
            try:
                for child in children:
                    _, error = child.communicate(timeout=10)
                    self.assertEqual(child.returncode, 0, error.decode())
                rows = read_trace(directory, 0)
                self.assertEqual(len(rows), 75)
                self.assertEqual(len({r["request_id"] for r in rows}), 75)
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.communicate()


class CoverageEvidenceTests(unittest.TestCase):
    def test_effect_inside_lost_response_interval_is_observed(self):
        self.assertIn("effect_applied_response_lost", observed(applied_run(error_type="ReadTimeout")))

    def test_received_5xx_after_commit_is_separate_from_lost_response(self):
        found = observed(applied_run("http_response", status_code=503))
        self.assertIn("server_error_after_commit", found)
        self.assertNotIn("effect_applied_response_lost", found)

    def test_effect_after_caller_timeout_does_not_prove_effect_before_response(self):
        raw = applied_run(error_type="ReadTimeout")
        raw["ledger"][2]["monotonic_ns"] = 60
        self.assertNotIn("effect_applied_response_lost", observed(raw))

    def test_concurrent_and_unrelated_calls_receive_no_false_credit(self):
        raw = applied_run(error_type="ReadTimeout")
        raw["ledger"].append(dict(raw["ledger"][1]))
        self.assertNotIn("effect_applied_response_lost", observed(raw))
        raw = applied_run(error_type="ReadTimeout")
        raw["ledger"][2]["correlation_sha256"] = correlation_hash("other")
        self.assertNotIn("effect_applied_response_lost", observed(raw))
        raw = applied_run(error_type="ReadTimeout")
        raw["http"].append(dict(raw["http"][-1]))
        self.assertNotIn("effect_applied_response_lost", observed(raw))

    def test_429_requires_a_later_request_that_reached_the_provider(self):
        raw = applied_run("http_response", status_code=429)
        raw["ledger"] = raw["ledger"][:2]
        later = [event("http_start", 60), event("http_response", 90, status_code=200)]
        for row in later:
            row["request_id"] = "r2"
        raw["http"].extend(later)
        self.assertNotIn("rate_limited_then_retried", observed(raw))
        raw["ledger"].append(event("call", 70))
        self.assertIn("rate_limited_then_retried", observed(raw))

    def test_empty_lookup_must_actually_precede_the_effect(self):
        raw = {"admissions": [], "ledger": [event("provider_start", 10),
               event("provider_end", 20, lookup_status="absent", lookup_records=[]), event("effect", 30)],
               "http": [event("http_start", 5), event("http_response", 25, status_code=200)]}
        for row in raw["ledger"][:2]:
            row["method"] = "lookup"
        for row in raw["http"]:
            row.update(method="GET", path="/v1/lookup")
        self.assertIn("late_commit_after_empty_lookup", observed(raw))
        raw["ledger"][1]["lookup_status"] = "in_progress"
        self.assertNotIn("late_commit_after_empty_lookup", observed(raw))
        raw["ledger"][1]["lookup_status"] = "absent"
        raw["ledger"][2]["monotonic_ns"] = 15
        self.assertNotIn("late_commit_after_empty_lookup", observed(raw))

    def test_lookup_corruption_requires_an_independently_known_effect(self):
        raw = applied_run("http_response", status_code=200)
        start, end = event("provider_start", 60), event("provider_end", 70,
            lookup_status="found", lookup_records=[dict(effect_id="re_one",
            payload_sha256=payload_hash({"amount": 99}))])
        for row in (start, end):
            row["method"], row["provider_call_id"] = "lookup", "p2"
        raw["ledger"].extend([start, end])
        self.assertIn("lookup_lied_outside_contract", observed(raw))
        end["lookup_records"][0]["effect_id"] = "unseen"
        self.assertNotIn("lookup_lied_outside_contract", observed(raw))


if __name__ == "__main__":
    unittest.main()
