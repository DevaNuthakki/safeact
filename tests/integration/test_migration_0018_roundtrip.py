"""Alembic 0018 upgrade/downgrade round trip on a throwaway database."""

import os
import unittest

from storage import durable_lifecycle_sql as lsql
from storage.lifecycle_sql import v0017 as OLD, v0018 as NEW
from tests.integration.migration_roundtrip import MigrationRoundTripCase


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class Migration0018RoundTripTests(MigrationRoundTripCase):

    def test_upgrade_downgrade_upgrade(self):
        self.migrate(self.upgrade, OLD.REVISION)
        before = self.functions()

        self.migrate(self.upgrade, NEW.REVISION)
        upgraded = self.functions()
        self.assertEqual(set(upgraded) - set(before), set(NEW.NEW_FUNCTIONS))
        for name in NEW.REPLACED_FUNCTIONS:
            self.assertNotEqual(upgraded[name][0], before[name][0])
            self.assertEqual(upgraded[name][1:], before[name][1:])
        for table in NEW.NEW_TABLES:
            self.assertIn(table, self.relations())

        self.migrate(self.downgrade, OLD.REVISION)
        self.assertEqual(self.functions(), before)
        for table in NEW.NEW_TABLES:
            self.assertNotIn(table, self.relations())

        self.migrate(self.upgrade, NEW.REVISION)
        self.assertEqual(self.functions(), upgraded)

    def test_downgrade_refuses_while_breaker_history_exists(self):
        self.migrate(self.upgrade, NEW.REVISION)
        self.execute("""
            INSERT INTO public.safeact_connector_health_events (
                connector_id, provider_account_id, event,
                consecutive_failures
            ) VALUES ('mock-payments', 'acct', 'breaker_opened', 5)
        """)
        with self.assertRaisesRegex(RuntimeError, "has history"):
            self.migrate(self.downgrade, OLD.REVISION)
        self.assertEqual(self.version(), [(NEW.REVISION,)])

    def test_autogenerate_never_proposes_dropping_head_objects(self):
        self.migrate(self.upgrade, "head")
        self.assertEqual(
            self.removals(lsql.autogenerate_include_object), set()
        )


if __name__ == "__main__":
    unittest.main()
