"""Alembic 0016 upgrade/downgrade round trip on a throwaway database."""

import os
import unittest

from storage import durable_lifecycle_sql as lsql
from storage.lifecycle_sql import v0015 as OLD, v0016 as NEW
from tests.integration.migration_roundtrip import MigrationRoundTripCase


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class Migration0016RoundTripTests(MigrationRoundTripCase):

    def test_upgrade_downgrade_upgrade(self):
        self.migrate(self.upgrade, OLD.REVISION)
        before = self.functions()

        self.migrate(self.upgrade, NEW.REVISION)
        upgraded = self.functions()
        self.assertEqual(set(upgraded) - set(before), set(NEW.NEW_FUNCTIONS))
        for name in NEW.REPLACED_FUNCTIONS:
            self.assertNotEqual(upgraded[name][0], before[name][0])
            self.assertEqual(upgraded[name][1:], before[name][1:])

        self.migrate(self.downgrade, OLD.REVISION)
        self.assertEqual(self.functions(), before)

        self.migrate(self.upgrade, NEW.REVISION)
        self.assertEqual(self.functions(), upgraded)

    def test_downgrade_refuses_while_an_agent_key_exists(self):
        self.migrate(self.upgrade, NEW.REVISION)
        self.execute("""
            INSERT INTO public.safeact_principals (
                principal_id, tenant_id, username, kind, display_name, demo
            ) VALUES (
                '00000000-0000-0000-0000-0000000000b1', 't', 'agent-one',
                'service', 'Agent', false
            );
            INSERT INTO public.safeact_agent_keys (
                key_id, principal_id, key_sha256, scopes, label, created_by,
                expires_at
            ) VALUES (
                gen_random_uuid(), '00000000-0000-0000-0000-0000000000b1',
                sha256('x'::bytea), '{submit}', 'test', 'test',
                now() + interval '1 day'
            )
        """)
        with self.assertRaisesRegex(RuntimeError, "Agent keys exist"):
            self.migrate(self.downgrade, OLD.REVISION)
        self.assertEqual(self.version(), [(NEW.REVISION,)])

    def test_autogenerate_never_proposes_dropping_head_objects(self):
        self.migrate(self.upgrade, "head")
        self.assertEqual(
            self.removals(lsql.autogenerate_include_object), set()
        )


if __name__ == "__main__":
    unittest.main()
