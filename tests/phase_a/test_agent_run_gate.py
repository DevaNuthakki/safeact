"""The paid-run gate (Gate 2): nothing paid starts unless every check holds.

No model, network or database: the run's preflight and manifest checks
are exercised directly with a temporary fake key file and spend ledger.
"""

import json
import os
import tempfile
import unittest

from pathlib import Path
from unittest import mock

from deploy.evaluation.agent_v1 import run, scenarios
from mcp_server.agent.budget import Price, SpendLedger, TrialLimits


ROOT = Path(__file__).resolve().parents[2] / "deploy" / "evaluation" / "agent_v1"
PAID_ARGS = ["--model", "openai", "--snapshot", "gpt-4.1-mini-2025-04-14",
             "--price-input", "0.40", "--price-cached", "0.10",
             "--price-output", "1.60", "--trial-usd", "0.04",
             "--max-output-tokens", "800", "--out", "/dev/null"]
TRIALS = {"dev": 1, "heldout": 3}


def paid_args(split, *extra):
    return run.parse(["--split", split, "--trials", str(TRIALS[split]),
                      *PAID_ARGS, *extra])


def built(args):
    limits = TrialLimits(max_turns=args.max_turns,
                         max_tool_calls=args.max_tool_calls,
                         max_output_tokens=args.max_output_tokens,
                         trial_usd=args.trial_usd)
    price = Price(args.price_input, args.price_cached, args.price_output)
    return run.build_manifest(args, limits, price), limits


class FrozenManifestTests(unittest.TestCase):

    def test_committed_manifests_match_what_the_code_builds(self):
        # A change to the prompt, tools, scenarios, scoring, budgets or SDK
        # versions breaks this until the manifest is re-frozen (and, for a
        # paid run, re-approved).
        for split in ("dev", "heldout"):
            with self.subTest(split=split):
                committed = json.loads((ROOT / f"manifest_{split}.json").read_text())
                manifest, _ = built(paid_args(split))
                self.assertEqual(committed, manifest)

    def test_manifests_record_the_snapshot_deprecation(self):
        for split in ("dev", "heldout"):
            lifecycle = json.loads(
                (ROOT / f"manifest_{split}.json").read_text())["model_lifecycle"]
            with self.subTest(split=split):
                self.assertEqual(lifecycle["deprecated_on"], "2026-10-14")
                self.assertEqual(lifecycle["shutdown_date"], "2027-01-14")
                self.assertEqual(lifecycle["replacement"], "gpt-6-luna")
                self.assertIn("cannot be rerun", lifecycle["note"])
                self.assertIn("model-agnostic", lifecycle["note"])

    def test_the_planned_worst_case_fits_the_total_cap(self):
        dev, heldout = scenarios.load("dev"), scenarios.load("heldout")
        trial_usd = 0.04
        worst = (len(dev) * TRIALS["dev"] + len(heldout) * TRIALS["heldout"]) * 2 * trial_usd
        self.assertLessEqual(worst, run.TOTAL_CAP_USD)
        self.assertGreaterEqual(TRIALS["heldout"], 3)


class PreflightTests(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.key = Path(self.directory.name) / "openai.key"
        self.key.write_text("sk-test-" + "Z" * 40 + "\n")
        os.chmod(self.key, 0o600)
        self.ledger = SpendLedger(Path(self.directory.name) / "spend.jsonl", 4.0)

    def env(self, **extra):
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("SAFEACT_ALLOW_PAID_MODEL", "CI", "GITHUB_ACTIONS",
                              "SAFEACT_OPENAI_KEY_FILE")}
        clean.update({"SAFEACT_OPENAI_KEY_FILE": str(self.key)}, **extra)
        return mock.patch.dict(os.environ, clean, clear=True)

    def preflight(self, split="heldout"):
        args = paid_args(split, "--manifest", str(ROOT / f"manifest_{split}.json"))
        _, limits = built(args)
        return run.preflight(args, limits, self.ledger, scenarios.load(split))

    def test_a_paid_run_needs_the_flag_and_never_runs_in_ci(self):
        with self.env(), self.assertRaises(SystemExit) as refused:
            self.preflight()
        self.assertIn("SAFEACT_ALLOW_PAID_MODEL", str(refused.exception))
        for ci in ({"CI": "true"}, {"GITHUB_ACTIONS": "true"}):
            with self.subTest(env=ci), self.env(SAFEACT_ALLOW_PAID_MODEL="1", **ci):
                with self.assertRaises(SystemExit):
                    self.preflight()

    def test_an_unsafe_key_file_refuses_before_anything_starts(self):
        os.chmod(self.key, 0o644)
        with self.env(SAFEACT_ALLOW_PAID_MODEL="1"), \
                self.assertRaises(SystemExit) as refused:
            self.preflight()
        self.assertNotIn("Z" * 10, str(refused.exception))

    def test_the_worst_case_must_fit_what_is_left_of_the_total_cap(self):
        with self.env(SAFEACT_ALLOW_PAID_MODEL="1"):
            model_trials, worst = self.preflight()
            self.assertEqual(model_trials, 12 * 3 * 2)
            self.assertAlmostEqual(worst, 2.88)
            self.ledger.record(run_id="calibration", trial="x", usd=1.20,
                               input_tokens=1, output_tokens=1)
            with self.assertRaises(SystemExit) as refused:
                self.preflight()
            self.assertIn("exceeds", str(refused.exception))

    def test_a_manifest_that_differs_is_refused(self):
        changed = json.loads((ROOT / "manifest_heldout.json").read_text())
        changed["model"] = "some-other-model"
        path = Path(self.directory.name) / "manifest.json"
        path.write_text(json.dumps(changed))
        args = paid_args("heldout", "--manifest", str(path))
        with self.env(SAFEACT_ALLOW_PAID_MODEL="1"), \
                self.assertRaises(SystemExit) as refused:
            run.run(args)
        self.assertIn("model", str(refused.exception))


class ExtensionRunTests(unittest.TestCase):
    """The pre-registered held-out extension: trials 4-6, same manifest."""

    def test_an_extension_run_matches_the_same_frozen_manifest(self):
        committed = json.loads((ROOT / "manifest_heldout.json").read_text())
        manifest, _ = built(paid_args("heldout", "--first-trial", "3"))
        self.assertEqual(committed, manifest)

    def test_extension_trials_are_new_trials_not_repeats(self):
        from deploy.evaluation.agent_v1.harness import make_trial
        scenario = scenarios.load("heldout")[0]
        original = {make_trial("direct_agent", scenario, i, "run").target for i in range(3)}
        extension = {make_trial("direct_agent", scenario, i, "run").target for i in range(3, 6)}
        self.assertEqual(len(original | extension), 6)

    def write_run(self, directory, run_id, trials, manifest_changes=None):
        from deploy.evaluation.agent_v1 import combine  # noqa: F401
        directory = Path(directory)
        directory.mkdir()
        manifest = {**json.loads((ROOT / "manifest_heldout.json").read_text()),
                    "run_id": run_id, "worst_case_usd": 1.0, "model_trials": 2,
                    "trial_indices": list(trials), **(manifest_changes or {})}
        (directory / "manifest.json").write_text(json.dumps(manifest))
        rows = [{"arm": "deterministic", "scenario_id": "AG1-H01", "split": "heldout",
                 "family": "no_fault_control", "trial": t, "target": "cust_x",
                 "prior_ref": None, "report": {"status": "succeeded", "summary": ""},
                 "stop_reason": "reported", "trace": [], "effects_on_target": 1,
                 "orphan_effects": {}, "model_turns": 1, "latency_seconds": 1.0,
                 "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
                for t in trials]
        (directory / "results.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))
        return directory

    def test_combining_counts_every_trial_once_under_one_manifest(self):
        from deploy.evaluation.agent_v1.combine import combine
        with tempfile.TemporaryDirectory() as directory:
            first = self.write_run(Path(directory) / "a", "run-a", range(3))
            second = self.write_run(Path(directory) / "b", "run-b", range(3, 6))
            report = combine(Path(directory) / "combined", [first, second])
            self.assertEqual(report["arms"]["deterministic"]["trials"], 6)
            self.assertEqual(report["manifest"]["trials_per_scenario"], 6)
            self.assertEqual(report["manifest"]["combined_runs"], ["run-a", "run-b"])
            self.assertTrue((Path(directory) / "combined" / "rescored.md").exists())

    def test_combining_refuses_repeats_or_different_manifests(self):
        from deploy.evaluation.agent_v1.combine import combine
        with tempfile.TemporaryDirectory() as directory:
            first = self.write_run(Path(directory) / "a", "run-a", range(3))
            repeat = self.write_run(Path(directory) / "b", "run-b", range(2, 5))
            with self.assertRaises(SystemExit) as refused:
                combine(Path(directory) / "out1", [first, repeat])
            self.assertIn("more than one run", str(refused.exception))
            other = self.write_run(Path(directory) / "c", "run-c", range(3, 6),
                                   {"scoring_version": "agent-eval-scoring-v1"})
            with self.assertRaises(SystemExit) as refused:
                combine(Path(directory) / "out2", [first, other])
            self.assertIn("one frozen manifest", str(refused.exception))


if __name__ == "__main__":
    unittest.main()
