"""Fail-closed SQL prototype construction tests."""

import unittest
from psycopg import sql

from storage.atomic_gate_sql import (
    create_gate_sql,
    gate_signature_sql,
    gate_call_sql,
)


class DisposableAtomicGateSqlTests(unittest.TestCase):

    def test_rejects_production_schema_names(self):
        for name in (
            "public",
            "safeact_dev",
            "safeact_it_invalid",
            "safeact_it_" + "A" * 32,
            "safeact_it_" + "0" * 31,
            'safeact_it_" DROP TABLE',
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    create_gate_sql(name)

    def test_rejects_non_string_schema(self):
        for value in (None, 123, {}, ("safeact_it_",)):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    gate_call_sql(value)

    def test_builders_return_safe_sql_composables(self):
        name = "safeact_it_" + "a" * 32

        for statement in (
            create_gate_sql(name),
            gate_signature_sql(name),
            gate_call_sql(name),
        ):
            self.assertIsInstance(statement, sql.Composable)


if __name__ == "__main__":
    unittest.main()
