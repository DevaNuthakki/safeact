"""The Stripe sandbox manifest is frozen, and a real run is gated."""

import json
import os
import unittest

from hashlib import sha256
from unittest import mock

from connectors.stripe_key import StripeSandboxRefused
from core.connector_profiles import STRIPE_API_VERSION
from deploy.sandbox import run_stripe


# Recorded when the manifest was frozen (before any real Stripe call).
# A change means a new manifest version, never an edit.
PINNED = "manifest_stripe_v1.json"


class ManifestTests(unittest.TestCase):

    def setUp(self):
        self.manifest = json.loads(run_stripe.MANIFEST.read_text("utf-8"))

    def test_manifest_matches_the_code_it_runs(self):
        self.assertEqual(run_stripe.MANIFEST.name, PINNED)
        self.assertEqual(self.manifest["stripe_version"], STRIPE_API_VERSION)
        self.assertEqual(self.manifest["profile"], run_stripe.RUN_PROFILE.row())
        profile = run_stripe.RUN_PROFILE
        self.assertEqual(self.manifest["limits"], profile.limits.row(
            profile.connector_id, profile.version))
        self.assertEqual(self.manifest["repeats"], 2)
        self.assertLessEqual(self.manifest["call_budget"], 150)

    def test_scenarios_are_unique_and_known(self):
        ids = [s["id"] for s in self.manifest["scenarios"]]
        self.assertEqual(len(ids), len(set(ids)))
        kinds = {"through", "through_twice", "replay", "changed_params",
                 "concurrent", "visibility"}
        for spec in self.manifest["scenarios"]:
            with self.subTest(scenario=spec["id"]):
                self.assertIn(spec["kind"], kinds)
                self.assertIn("stripe_refunds", spec["expect"])

    def test_the_digest_is_stable(self):
        digest = run_stripe.manifest_digest(self.manifest)
        self.assertEqual(digest, sha256(json.dumps(
            self.manifest, sort_keys=True).encode()).hexdigest())

    def test_a_real_run_needs_the_gate(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(StripeSandboxRefused):
                run_stripe.main(["--out", "/nonexistent/never-written"])
        with mock.patch.dict(os.environ, {"SAFEACT_ALLOW_STRIPE_SANDBOX": "1",
                                          "GITHUB_ACTIONS": "true"}, clear=True):
            with self.assertRaises(StripeSandboxRefused):
                run_stripe.main(["--out", "/nonexistent/never-written"])

    def test_check_reports_every_difference(self):
        spec = {"expect": {"state": "SUCCEEDED", "a.b": 1, "x": "observed",
                           "y": {"one_of": [1, 2]}}}
        self.assertEqual(run_stripe.check(spec, {
            "state": "SUCCEEDED", "a": {"b": 1}, "y": 2}), [])
        failures = run_stripe.check(spec, {"state": "UNKNOWN", "a": {},
                                           "y": 3})
        self.assertEqual({f["field"] for f in failures}, {"state", "a.b", "y"})


if __name__ == "__main__":
    unittest.main()
