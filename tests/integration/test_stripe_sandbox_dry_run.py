"""The Stripe sandbox runner, end to end, offline against the fake.

Runs deploy/sandbox/run_stripe.py --fake: the frozen manifest's
scenarios through SafeAct's real admission, relay, worker and reconciler
in a disposable schema, with the Stripe adapter talking to the
documented-behaviour fake. Never touches the network or Stripe, so it
runs in CI. Asserted: every scenario passes, the output is free of
secret-shaped text, and no call carries more than a key tag.
"""

import json
import os
import tempfile
import unittest

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from deploy.evaluation.agent_v1.secret_scan import scan
from deploy.sandbox import run_stripe


ENABLED = (
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1"
)


@unittest.skipUnless(ENABLED, "Explicit disposable PostgreSQL opt-in required")
class StripeSandboxDryRunTests(unittest.TestCase):

    def test_frozen_scenarios_pass_offline(self):
        with tempfile.TemporaryDirectory() as out:
            code = run_stripe.main(["--fake", "--out", out])
            summary = json.loads((Path(out) / "summary.json").read_text())
            rows = [json.loads(line) for line in
                    (Path(out) / "results.jsonl").read_text().splitlines()]
            calls = [json.loads(line) for line in
                     (Path(out) / "calls.jsonl").read_text().splitlines()]
            findings = scan([out])

        self.assertEqual(code, 0)
        self.assertEqual(summary["results"],
                         {"PASS": len(rows), "DIFFERENT": 0, "ERROR": 0})
        self.assertTrue(summary["mode"].startswith("fake"))
        self.assertLessEqual(summary["calls"], summary["call_budget"])
        self.assertEqual(findings, {})
        for call in calls:
            self.assertEqual(set(call) - {
                "method", "path", "key_tag", "at", "latency_ms", "status",
                "request_id", "replayed", "error",
            }, set())
        lost = [r for r in rows if r["scenario"] == "ST-02"]
        for row in lost:
            self.assertIn("ambiguous_no_response", row["observed"]["reasons"])
            self.assertEqual(row["observed"]["refund_posts"], 1)


    def _synthetic_failed_run(self, **scenario_patch):
        with tempfile.TemporaryDirectory() as out:
            with mock.patch.object(run_stripe.Run, "scenario", **scenario_patch):
                with redirect_stdout(StringIO()):
                    code = run_stripe.main(["--fake", "--out", out])
            summary = json.loads((Path(out) / "summary.json").read_text())
            rows = [json.loads(line) for line in
                    (Path(out) / "results.jsonl").read_text().splitlines()]
            findings = scan([out])
        return code, summary, rows, findings

    def test_differences_fail_the_run_and_preserve_evidence(self):
        code, summary, rows, findings = self._synthetic_failed_run(return_value={})
        self.assertNotEqual(code, 0)
        self.assertEqual(summary["results"],
                         {"PASS": 0, "DIFFERENT": 16, "ERROR": 0})
        self.assertEqual(len(rows), 16)
        self.assertTrue(all(r["result"] == "DIFFERENT" and r["differences"]
                            and r["error"] is None for r in rows))
        self.assertEqual(findings, {})

    def test_errors_fail_the_run_and_preserve_evidence(self):
        code, summary, rows, findings = self._synthetic_failed_run(
            side_effect=RuntimeError("synthetic offline failure"))
        self.assertNotEqual(code, 0)
        self.assertEqual(summary["results"],
                         {"PASS": 0, "DIFFERENT": 0, "ERROR": 16})
        self.assertEqual(len(rows), 16)
        self.assertTrue(all(r["result"] == "ERROR" and not r["differences"]
                            and r["error"] == "RuntimeError" for r in rows))
        self.assertEqual(findings, {})


if __name__ == "__main__":
    unittest.main()
