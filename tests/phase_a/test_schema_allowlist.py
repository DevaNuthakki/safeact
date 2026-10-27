"""The lifecycle schema allowlist admits exactly one extra name.

``v0003._schema`` is the single schema-name gate for every frozen
migration. The only edit ever made to a frozen module was adding the
exact name ``safeact_demo`` (the local demo schema); rendered SQL for
every migration is unchanged, which the golden-digest tests prove.
"""

import unittest

from storage import durable_lifecycle_sql as lsql
from storage.lifecycle_sql import v0003


class SchemaAllowlistTests(unittest.TestCase):

    def test_exact_names_are_accepted(self):
        for name in (
            "public",
            "safeact_demo",
            "safeact_it_" + "0" * 32,
            "safeact_it_" + "ab" * 16,
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    v0003._schema(name).as_string(None), f'"{name}"'
                )

    def test_near_misses_are_rejected(self):
        for name in (
            "safeact_demo2",
            "safeact_demo_",
            "SAFEACT_DEMO",
            "Safeact_demo",
            "safeact_demo ",
            " safeact_demo",
            "safeact_demo\n",
            "safeact_demo\x00",
            "safeact_dem",
            '"safeact_demo"',
            "safeact_demo'",
            "safeact_demo;",
            "safeact_demo; DROP SCHEMA public",
            "safeact_demo.public",
            "safeact_it_" + "0" * 31,
            "safeact_it_" + "0" * 33,
            "safeact_it_" + "G" * 32,
            "",
            None,
            b"safeact_demo",
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    v0003._schema(name)

    def test_every_migration_renders_for_the_demo_schema(self):
        # The demo schema goes through the same frozen chain as any
        # disposable schema; only the identifier differs.
        disposable = "safeact_it_" + "0" * 32
        demo = lsql.upgrade_statements("safeact_demo")
        same = lsql.upgrade_statements(disposable)
        self.assertEqual(len(demo), len(same))
        for ours, theirs in zip(demo, same):
            self.assertEqual(
                ours.as_string(None).replace('"safeact_demo"', "S"),
                theirs.as_string(None).replace(f'"{disposable}"', "S"),
            )


if __name__ == "__main__":
    unittest.main()
