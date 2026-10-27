"""Regression tests for the conservative fixture-only evaluator."""

import copy
import hashlib
import json
import unittest

from deploy.evaluation import run_corpus_v1 as runner


class EvaluationRunnerV1Tests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.records = runner.load_corpus()

    @classmethod
    def scenario(cls, scenario_id):
        matches = [
            row
            for row in cls.records
            if row["scenario_id"] == scenario_id
        ]

        if len(matches) != 1:
            raise AssertionError(
                "Unexpected scenario inventory"
            )

        return matches[0]

    def test_pinned_corpus_and_design_only_status(self):
        self.assertEqual(
            hashlib.sha256(
                runner.CORPUS.read_bytes()
            ).hexdigest(),
            runner.CORPUS_SHA256,
        )

        self.assertEqual(len(self.records), 10)

        for record in self.records:
            self.assertEqual(
                record["partition"], "design"
            )
            self.assertEqual(
                record["execution_status"], "NOT_RUN"
            )
            self.assertEqual(
                record["oracle_implementation"],
                "UNIMPLEMENTED",
            )

    def test_exact_development_adapter_allowlist(self):
        self.assertEqual(
            set(runner.ADAPTERS),
            set(runner.EXPECTED_FIELDS),
        )

        self.assertEqual(
            set(runner.ADAPTERS),
            {
                "SA-EVAL-001",
                "SA-EVAL-002",
                "SA-EVAL-005",
                "SA-EVAL-006",
            },
        )

    def test_simulated_late_commit_against_independent_ledger(self):
        record = self.scenario("SA-EVAL-001")
        actual = runner.observe_001()

        verdict, _, details = runner.grade(
            record, actual
        )

        self.assertEqual(verdict, "PASS")
        self.assertEqual(len(details), 4)

        self.assertEqual(
            actual[("provider_oracle", "effect_count")],
            1,
        )
        self.assertEqual(
            actual[("provider_oracle", "dispatch_count")],
            1,
        )

    def test_empty_inventory_conservatively_holds(self):
        record = self.scenario("SA-EVAL-002")
        actual = runner.observe_002()

        self.assertEqual(
            runner.grade(record, actual)[0],
            "PASS",
        )

        self.assertIs(
            actual[("policy_decision", "retry_allowed")],
            False,
        )

    def test_missing_terminal_proof_rejected(self):
        record = self.scenario("SA-EVAL-005")

        self.assertEqual(
            runner.grade(
                record,
                runner.observe_005(),
            )[0],
            "PASS",
        )

    def test_exhausted_retry_budget_blocks_retry(self):
        record = self.scenario("SA-EVAL-006")
        actual = runner.observe_006()

        self.assertEqual(
            runner.grade(record, actual)[0],
            "PASS",
        )

        self.assertIs(
            actual[("policy_decision", "retry_allowed")],
            False,
        )

    def test_six_cases_remain_not_run(self):
        report = runner.evaluate(self.records)

        self.assertEqual(
            report["totals"],
            {
                "PASS": 4,
                "FAIL": 0,
                "INCONCLUSIVE": 0,
                "NOT_RUN": 6,
            },
        )

        not_run = {
            item["scenario_id"]
            for item in report["results"]
            if item["verdict"] == "NOT_RUN"
        }

        self.assertEqual(
            not_run,
            {
                "SA-EVAL-003",
                "SA-EVAL-004",
                "SA-EVAL-007",
                "SA-EVAL-008",
                "SA-EVAL-009",
                "SA-EVAL-010",
            },
        )

        self.assertIs(
            report["production_approved"], False
        )

        self.assertIs(
            report["final_holdout"], False
        )

    def test_changed_expected_decision_causes_fail(self):
        record = copy.deepcopy(
            self.scenario("SA-EVAL-002")
        )

        record["assertions"][0]["equals"] = (
            "RETRY_SAME_KEY"
        )

        self.assertEqual(
            runner.grade(
                record,
                runner.observe_002(),
            )[0],
            "FAIL",
        )

    def test_changed_metric_contract_becomes_inconclusive(self):
        record = copy.deepcopy(
            self.scenario("SA-EVAL-002")
        )

        record["assertions"][0]["metric"] = (
            "final_decision"
        )

        verdict, reason, _ = runner.grade(
            record,
            runner.observe_002(),
        )

        self.assertEqual(
            (verdict, reason),
            (
                "INCONCLUSIVE",
                "unexpected_expected_metric_contract",
            ),
        )

    def test_missing_observation_becomes_inconclusive(self):
        record = self.scenario("SA-EVAL-002")

        verdict, reason, _ = runner.grade(
            record, {}
        )

        self.assertEqual(
            (verdict, reason),
            (
                "INCONCLUSIVE",
                "missing_or_extra_observation",
            ),
        )

    def test_bool_integer_type_mismatch_is_failure(self):
        record = self.scenario("SA-EVAL-002")
        actual = runner.observe_002()

        actual[(
            "policy_decision",
            "retry_allowed",
        )] = 0

        self.assertEqual(
            runner.grade(record, actual)[0],
            "FAIL",
        )

    def test_adapter_exception_never_passes(self):
        def broken():
            raise RuntimeError(
                "injected fixture failure"
            )

        adapters = {
            **runner.ADAPTERS,
            "SA-EVAL-002": broken,
        }

        report = runner.evaluate(
            self.records,
            adapters=adapters,
        )

        item = next(
            entry
            for entry in report["results"]
            if entry["scenario_id"] == "SA-EVAL-002"
        )

        self.assertEqual(
            item["verdict"],
            "INCONCLUSIVE",
        )
        self.assertEqual(
            report["totals"]["PASS"], 3
        )
        self.assertEqual(
            report["totals"]["INCONCLUSIVE"], 1
        )

        json.dumps(report, sort_keys=True)


if __name__ == "__main__":
    unittest.main()
