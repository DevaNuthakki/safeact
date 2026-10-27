"""Structural and provenance tests for SafeAct corpus design v1."""

import ast
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "deploy/evaluation/corpus_v1.jsonl"

FIELDS = {
    "scenario_id", "title", "category", "priority",
    "partition", "evidence_status", "source_kind",
    "execution_status", "oracle_implementation",
    "preconditions", "events", "assertions",
    "reference_tests", "limitation",
}

OBSERVERS = {
    "policy_decision": {
        "initial_decision", "final_decision",
        "retry_allowed",
    },
    "provider_oracle": {
        "effect_count", "dispatch_count",
    },
    "scheduler_trace": {
        "extra_dispatches",
    },
    "database_snapshot": {
        "bundle_counts", "nonce_count",
    },
    "validator_output": {"status"},
    "sql_gate": {"status"},
}

EVIDENCE = {
    "SA-EVAL-001": "reference_reviewed",
    "SA-EVAL-002": "reference_reviewed",
    "SA-EVAL-003": "partial_reference",
    "SA-EVAL-004": "partial_reference",
    "SA-EVAL-005": "reference_reviewed",
    "SA-EVAL-006": "reference_reviewed",
    "SA-EVAL-007": "partial_reference",
    "SA-EVAL-008": "reference_reviewed",
    "SA-EVAL-009": "reference_reviewed",
    "SA-EVAL-010": "partial_reference",
}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


def reject_constant(_value):
    raise ValueError("Non-finite JSON number")


class EvaluationCorpusContractTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.lines = CORPUS.read_text(
            encoding="utf-8"
        ).splitlines()

        cls.records = [
            json.loads(
                line,
                object_pairs_hook=unique_object,
                parse_constant=reject_constant,
            )
            for line in cls.lines
        ]

    def test_exact_initial_scenario_ids(self):
        actual = [
            record["scenario_id"]
            for record in self.records
        ]

        self.assertEqual(
            actual,
            [
                f"SA-EVAL-{i:03d}"
                for i in range(1, 11)
            ],
        )

        self.assertEqual(len(actual), len(set(actual)))

    def test_explicit_schema_and_lifecycle(self):
        for record in self.records:
            with self.subTest(id=record["scenario_id"]):
                self.assertEqual(set(record), FIELDS)
                self.assertEqual(record["priority"], "P0")
                self.assertEqual(record["partition"], "design")
                self.assertEqual(
                    record["execution_status"], "NOT_RUN"
                )
                self.assertEqual(
                    record["oracle_implementation"],
                    "UNIMPLEMENTED",
                )

                for name in (
                    "title", "category", "source_kind",
                    "limitation",
                ):
                    self.assertIsInstance(record[name], str)
                    self.assertTrue(record[name].strip())

    def test_scenario_substance(self):
        for record in self.records:
            with self.subTest(id=record["scenario_id"]):
                self.assertGreaterEqual(
                    len(record["preconditions"]), 2
                )
                self.assertGreaterEqual(
                    len(record["events"]), 2
                )
                self.assertTrue(record["assertions"])

                for item in (
                    record["preconditions"]
                    + record["events"]
                ):
                    self.assertIsInstance(item, str)
                    self.assertGreater(len(item.strip()), 12)

    def test_oracle_assertion_structure(self):
        for record in self.records:
            signatures = set()

            for assertion in record["assertions"]:
                self.assertEqual(
                    set(assertion),
                    {"observer", "metric", "equals"},
                )

                observer = assertion["observer"]
                metric = assertion["metric"]

                self.assertIn(observer, OBSERVERS)
                self.assertIn(metric, OBSERVERS[observer])

                signature = (observer, metric)
                self.assertNotIn(signature, signatures)
                signatures.add(signature)

                value = assertion["equals"]

                self.assertIn(
                    type(value),
                    (str, int, bool, list),
                )

                if metric == "bundle_counts":
                    self.assertEqual(len(value), 4)
                    self.assertTrue(all(
                        type(n) is int and n >= 0
                        for n in value
                    ))

                if metric.endswith("_count") or metric in {
                    "effect_count", "dispatch_count",
                    "extra_dispatches", "nonce_count",
                }:
                    self.assertIs(type(value), int)
                    self.assertGreaterEqual(value, 0)

                if metric == "retry_allowed":
                    self.assertIs(type(value), bool)

    def test_referenced_methods_exist(self):
        parsed = {}

        for record in self.records:
            for reference in record["reference_tests"]:
                path_text, separator, qualname = (
                    reference.partition("::")
                )

                self.assertEqual(separator, "::")
                self.assertTrue(path_text.startswith("tests/"))
                self.assertNotIn("..", Path(path_text).parts)

                path = ROOT / path_text
                self.assertTrue(path.is_file())

                if path_text not in parsed:
                    parsed[path_text] = ast.parse(
                        path.read_text(encoding="utf-8"),
                        filename=path_text,
                    )

                cls_name, dot, method_name = (
                    qualname.partition(".")
                )

                self.assertEqual(dot, ".")
                self.assertTrue(
                    method_name.startswith("test_")
                )

                matching = [
                    method
                    for cls in parsed[path_text].body
                    if isinstance(cls, ast.ClassDef)
                    and cls.name == cls_name
                    for method in cls.body
                    if isinstance(
                        method,
                        (ast.FunctionDef, ast.AsyncFunctionDef),
                    )
                    and method.name == method_name
                ]

                self.assertEqual(
                    len(matching), 1,
                    msg=reference,
                )

    def test_evidence_labels_remain_conservative(self):
        actual = {
            record["scenario_id"]:
                record["evidence_status"]
            for record in self.records
        }

        self.assertEqual(actual, EVIDENCE)

        for record in self.records:
            if record["evidence_status"] == "not_established":
                self.assertEqual(
                    record["reference_tests"], []
                )
            else:
                self.assertTrue(
                    record["reference_tests"]
                )

        self.assertEqual(
            {
                record["category"]
                for record in self.records
            }.__len__(),
            10,
        )


    def test_lost_ack_provenance_remains_partial(self):
        record = next(
            row for row in self.records
            if row["scenario_id"] == "SA-EVAL-003"
        )

        self.assertEqual(
            record["evidence_status"],
            "partial_reference",
        )
        self.assertEqual(
            record["source_kind"],
            "postgres_transaction",
        )
        self.assertEqual(
            record["partition"],
            "design",
        )
        self.assertEqual(
            record["execution_status"],
            "NOT_RUN",
        )
        self.assertEqual(
            record["oracle_implementation"],
            "UNIMPLEMENTED",
        )

        self.assertEqual(
            record["reference_tests"],
            [
                "tests/integration/test_submission_commits.py::CommittedSubmissionTests.test_injected_lost_ack_after_real_commit",
                "tests/integration/test_submission_commits.py::CommittedSubmissionTests.test_inflight_commit_not_found_stays_unknown",
                "tests/integration/test_submission_commits.py::CommittedSubmissionTests.test_observe_committed_bundle",
                "tests/integration/test_submission_commits.py::CommittedSubmissionTests.test_observation_read_failure_is_not_confirmed",
            ],
        )

        self.assertIn(
            "synthetic ConnectionError",
            record["limitation"],
        )
        self.assertIn(
            "No real network interruption",
            record["limitation"],
        )
        self.assertIn(
            "No independent retry-authorization observation",
            record["limitation"],
        )

        self.assertEqual(
            record["assertions"],
            [
                {
                    "observer": "policy_decision",
                    "metric": "retry_allowed",
                    "equals": False,
                },
                {
                    "observer": "database_snapshot",
                    "metric": "bundle_counts",
                    "equals": [1, 0, 1, 1],
                },
            ],
        )

    def test_canonical_json_lines(self):
        self.assertEqual(len(self.lines), 10)

        for line, record in zip(
            self.lines, self.records
        ):
            self.assertEqual(
                line,
                json.dumps(
                    record,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ),
            )

        raw = CORPUS.read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"\r", raw)


if __name__ == "__main__":
    unittest.main()
