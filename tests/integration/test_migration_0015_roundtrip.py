"""Alembic 0015 upgrade/downgrade round trip on a throwaway database."""

import os
import unittest

from storage import durable_lifecycle_sql as lsql
from storage.lifecycle_sql import v0014 as OLD, v0015 as NEW
from tests.integration.migration_roundtrip import MigrationRoundTripCase


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class Migration0015RoundTripTests(MigrationRoundTripCase):

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

    def test_downgrade_refuses_after_an_expired_approval(self):
        self.migrate(self.upgrade, NEW.REVISION)
        self.execute("""
            INSERT INTO public.safeact_operations (
                operation_id, tenant_id, request_key, action_type,
                resource_id, provider_account_id, connector_id,
                contract_version, payload_sha256, payload_canonical
            ) VALUES (
                '00000000-0000-0000-0000-0000000000a1', 't', 'k', 'a', 'r',
                'p', 'mock-crm', 'v1',
                encode(sha256(convert_to('{}', 'UTF8')), 'hex'), '{}'
            );
            INSERT INTO public.safeact_operation_transitions (
                operation_id, seq, from_state, to_state, claim_id, actor,
                reason
            ) VALUES (
                '00000000-0000-0000-0000-0000000000a1', 1, 'RECORDED',
                'CANCELLED_BEFORE_CLAIM', gen_random_uuid(), 'a',
                'retry_approval_expired'
            )
        """)
        with self.assertRaisesRegex(RuntimeError, "follow-up history"):
            self.migrate(self.downgrade, OLD.REVISION)
        self.assertEqual(self.version(), [(NEW.REVISION,)])

    def test_autogenerate_never_proposes_dropping_head_objects(self):
        self.migrate(self.upgrade, "head")
        self.assertEqual(
            self.removals(lsql.autogenerate_include_object), set()
        )


if __name__ == "__main__":
    unittest.main()
