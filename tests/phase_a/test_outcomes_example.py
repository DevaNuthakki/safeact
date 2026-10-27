"""Customer-facing counts retain denominators, unresolved cases and scope."""

import json
import unittest
from copy import deepcopy
from pathlib import Path

from examples.outcomes import render


RESULTS = Path(__file__).resolve().parents[2] / "docs/phase-e/dev-workflows-20261022/results.jsonl"


class OutcomesExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = [json.loads(line) for line in RESULTS.read_text().splitlines()]

    def test_original_workflows_keep_unresolved_outcomes_outside_useful_completion(self):
        report = render(self.rows, family="F13")
        self.assertIn("| safeact | 5 | 2/2 | 1 | 1 | 3 | 0 |", report)
        self.assertIn("36 calls (15 dispatch, 21 lookup)", report)
        self.assertIn("NOT_RUN", report)
        self.assertIn("not counts of human reviews performed", report)

    def test_harness_failure_keeps_achievable_denominator(self):
        rows = deepcopy(self.rows)
        next(row for row in rows if row["scenario_id"] == "D-F13-0000")["harness_errors"] = ["lost trace"]
        self.assertIn("| safeact | 5 | 1/2 | 1 | 1 | 3 | 1 |", render(rows, family="F13"))

    def test_na_is_disclosed_and_never_counted_as_completed(self):
        rows = deepcopy(self.rows)
        rows[0]["not_applicable"] = True
        report = render(rows)
        self.assertIn("| safeact | 7 | 4/4 |", report)
        self.assertIn("N/A rows: 1", report)

    def test_missing_requested_denominator_is_refused(self):
        rows = deepcopy(self.rows)
        del rows[0]["expected_operations"]
        with self.assertRaises(ValueError):
            render(rows)

    def test_repeated_or_empty_samples_are_refused(self):
        for rows in ([], [self.rows[0], self.rows[0]]):
            with self.subTest(rows=len(rows)), self.assertRaises(ValueError):
                render(rows)

    def test_confirmed_failures_are_visible_as_other_outcomes(self):
        rows = deepcopy(self.rows)
        rows[0]["operations"][0]["outcome_class"] = "confirmed_failure"
        self.assertIn("confirmed failure = 1", render(rows))


if __name__ == "__main__":
    unittest.main()
