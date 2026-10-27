"""Metrics stay low-cardinality and identifier-free (M5.7)."""

import ast
import re
import unittest

from pathlib import Path

from observability import metrics
from storage import durable_lifecycle as L


ROOT = Path(__file__).resolve().parents[2]
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-")


class MetricDefinitionTests(unittest.TestCase):

    def test_names_and_labels(self):
        for metric in metrics.METRICS:
            with self.subTest(metric=metric._name):
                self.assertTrue(metric._name.startswith("safeact_"))
                self.assertLessEqual(set(metric._labelnames),
                                     metrics.ALLOWED_LABELS)
        for name, (_, label) in metrics.DATABASE_GAUGES.items():
            self.assertIn(label, metrics.ALLOWED_LABELS | {None})

    def test_no_identifier_label_is_allowed(self):
        for forbidden in ("tenant", "tenant_id", "operation_id", "principal",
                          "key", "provider_key", "resource_id", "path",
                          "user", "attempt_id", "claim_id"):
            self.assertNotIn(forbidden, metrics.ALLOWED_LABELS)

    def test_label_values_come_from_safeact_vocabularies(self):
        # Every .labels(...) call site passes vocabulary values: connector
        # ids, kinds, reasons, states, route templates; never ids.
        for relative in ("storage/experimental_durable_execution.py",
                         "storage/experimental_recovery_worker.py",
                         "api/app.py", "deploy/demo/run_stack.py"):
            tree = ast.parse((ROOT / relative).read_text("utf-8"))
            with self.subTest(module=relative):
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Call)
                            and getattr(node.func, "attr", "") == "labels"):
                        text = ast.unparse(node)
                        self.assertNotRegex(
                            text, r"operation_id|tenant|principal|url\.path")

    def test_refusal_reasons(self):
        self.assertEqual(metrics.refused_reason(L.NotYetDue("x")), "not_due")
        self.assertEqual(metrics.refused_reason(L.ProviderLimited("x")),
                         "provider_limited")
        self.assertEqual(metrics.refused_reason(L.StaleTransition("x")),
                         "already_claimed")
        self.assertEqual(metrics.refused_reason(ValueError()), "other")


class ExportTests(unittest.TestCase):

    def test_nothing_is_exported_unless_asked(self):
        self.assertIsNone(metrics.serve(env={}))

    def test_the_database_collector_survives_an_outage(self):
        def broken():
            raise OSError("database down")

        families = list(metrics.DatabaseCollector(broken, "x").collect())
        self.assertEqual(families, [])

    def test_the_database_collector_maps_rows(self):
        class Connection:
            def execute(self, *_):
                return self

            def fetchall(self):
                return [("operations", "SUCCEEDED", 3), ("claimable", "", 2),
                        ("unknown_metric", "", 9)]

            def rollback(self):
                pass

            def close(self):
                pass

        families = {f.name: f for f in metrics.DatabaseCollector(
            Connection, "safeact_demo").collect()}
        samples = {(f, s.labels.get("state"), s.value)
                   for f, family in families.items() for s in family.samples}
        self.assertIn(("safeact_operations", "SUCCEEDED", 3.0), samples)
        self.assertIn(("safeact_claimable", None, 2.0), samples)
        self.assertNotIn("safeact_unknown_metric", families)


if __name__ == "__main__":
    unittest.main()
