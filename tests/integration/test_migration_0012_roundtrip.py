"""Alembic 0012 upgrade/downgrade round trip on a throwaway database."""

import os
import unittest

from uuid import uuid4

from storage import durable_lifecycle_sql as lsql
from storage.lifecycle_sql import v0011 as OLD, v0012 as NEW
from tests.integration.migration_roundtrip import MigrationRoundTripCase


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class Migration0012RoundTripTests(MigrationRoundTripCase):

    def pairs(self):
        return set(self.query(
            "SELECT from_state, to_state FROM safeact_legal_transitions"
        ))

    def test_upgrade_downgrade_upgrade(self):
        self.migrate(self.upgrade, OLD.REVISION)
        before, before_pairs = self.functions(), self.pairs()

        self.migrate(self.upgrade, NEW.REVISION)
        upgraded = self.functions()
        self.assertEqual(set(upgraded) - set(before), set(NEW.NEW_FUNCTIONS))
        for name in NEW.REPLACED_FUNCTIONS:
            self.assertNotEqual(upgraded[name][0], before[name][0])
            self.assertEqual(upgraded[name][1:], before[name][1:])
        self.assertEqual(self.pairs() - before_pairs, set(NEW.LEGAL_TRANSITIONS))

        self.migrate(self.downgrade, OLD.REVISION)
        self.assertEqual(self.functions(), before)
        self.assertEqual(self.pairs(), before_pairs)

        self.migrate(self.upgrade, NEW.REVISION)
        self.assertEqual(self.functions(), upgraded)

    def test_downgrade_refuses_while_a_cancellation_exists(self):
        self.migrate(self.upgrade, NEW.REVISION)
        operation = uuid4()
        self.execute("""
            INSERT INTO public.safeact_operations (
                operation_id, tenant_id, request_key, action_type,
                resource_id, provider_account_id, connector_id,
                contract_version, payload_sha256, payload_canonical
            ) VALUES (
                :id, 't', 'k', 'a', 'r', 'p', 'mock-crm', 'v1',
                encode(sha256(convert_to('{}', 'UTF8')), 'hex'), '{}'
            )
        """, {"id": operation})
        self.execute("""
            INSERT INTO public.safeact_operation_transitions (
                operation_id, seq, from_state, to_state, claim_id, actor,
                reason
            ) VALUES (
                :id, 1, 'RECORDED', 'CANCELLED_BEFORE_CLAIM',
                gen_random_uuid(), 'a', 'cancelled_before_claim'
            )
        """, {"id": operation})

        with self.assertRaisesRegex(RuntimeError, "Cancelled operations"):
            self.migrate(self.downgrade, OLD.REVISION)
        self.assertEqual(self.version(), [(NEW.REVISION,)])

    def test_autogenerate_never_proposes_dropping_head_objects(self):
        self.migrate(self.upgrade, "head")
        self.assertEqual(
            self.removals(lsql.autogenerate_include_object), set()
        )


if __name__ == "__main__":
    unittest.main()
