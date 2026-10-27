"""Alembic 0011 upgrade/downgrade round trip on a throwaway database."""

import os
import unittest

from storage import durable_lifecycle_sql as lsql
from storage.lifecycle_sql import v0010 as OLD, v0011 as NEW
from tests.integration.migration_roundtrip import MigrationRoundTripCase


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class Migration0011RoundTripTests(MigrationRoundTripCase):

    def test_upgrade_downgrade_upgrade(self):
        self.migrate(self.upgrade, OLD.REVISION)
        before = self.functions()

        self.migrate(self.upgrade, NEW.REVISION)
        upgraded = self.functions()
        self.assertEqual(set(upgraded) - set(before), set(NEW.NEW_FUNCTIONS))
        for name in before:
            self.assertEqual(upgraded[name], before[name])

        self.migrate(self.downgrade, OLD.REVISION)
        self.assertEqual(self.functions(), before)

        self.migrate(self.upgrade, NEW.REVISION)
        self.assertEqual(self.functions(), upgraded)

    def test_public_holds_no_principal_and_refuses_demo_ones(self):
        self.migrate(self.upgrade, NEW.REVISION)
        with self.assertRaisesRegex(Exception, "Demo principals"):
            self.execute("""
                INSERT INTO public.safeact_principals (
                    principal_id, tenant_id, username, kind,
                    display_name, demo
                ) VALUES (
                    gen_random_uuid(), 't', 'demo-north-x', 'human', 'X', true
                )
            """)

    def test_downgrade_refuses_while_identity_history_exists(self):
        self.migrate(self.upgrade, NEW.REVISION)
        self.execute("""
            INSERT INTO public.safeact_principals (
                principal_id, tenant_id, username, kind, display_name, demo
            ) VALUES (
                gen_random_uuid(), 't', 'ops-person', 'human', 'Ops', false
            )
        """)

        with self.assertRaisesRegex(RuntimeError, "Identity history exists"):
            self.migrate(self.downgrade, OLD.REVISION)
        self.assertEqual(self.version(), [(NEW.REVISION,)])

    def test_autogenerate_never_proposes_dropping_head_objects(self):
        self.migrate(self.upgrade, "head")
        self.assertEqual(
            self.removals(lsql.autogenerate_include_object), set()
        )


if __name__ == "__main__":
    unittest.main()
