"""Alembic 0008 round trip and its preflight refusals."""

import os
import unittest

from uuid import uuid4

from core.provider_keys import derive_provider_key
from storage import durable_lifecycle_sql as lsql
from storage.lifecycle_sql import v0007 as OLD, v0008 as NEW
from tests.integration.migration_roundtrip import MigrationRoundTripCase


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class Migration0008RoundTripTests(MigrationRoundTripCase):

    def insert(self, request_key, key, account="acct"):
        self.execute("""
            INSERT INTO public.safeact_operations (
                operation_id, tenant_id, request_key, action_type,
                resource_id, provider_account_id, connector_id,
                contract_version, payload_sha256, payload_canonical,
                provider_idempotency_key
            ) VALUES (
                :id, 't', :rk, 'refund_payment', 'r', :acct,
                'mock-payments', 'v1',
                encode(sha256(convert_to('{}', 'UTF8')), 'hex'), '{}', :key
            )
        """, {"id": uuid4(), "rk": request_key, "acct": account, "key": key})

    def constraints(self):
        return set(self.query("""
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'public.safeact_operations'::regclass
        """))

    def test_upgrade_downgrade_upgrade(self):
        self.migrate(self.upgrade, OLD.REVISION)
        before = self.functions()
        constraints = self.constraints()

        self.migrate(self.upgrade, NEW.REVISION)
        upgraded = self.functions()
        self.assertEqual(
            self.constraints() - constraints, {(NEW.KEY_CONSTRAINT,)}
        )
        self.assertEqual(self.query(
            "SELECT indexname FROM pg_indexes WHERE indexname = :n",
            {"n": NEW.KEY_INDEX},
        ), [(NEW.KEY_INDEX,)])

        self.migrate(self.downgrade, OLD.REVISION)
        self.assertEqual(self.functions(), before)
        self.assertEqual(self.constraints(), constraints)

        self.migrate(self.upgrade, NEW.REVISION)
        self.assertEqual(self.functions(), upgraded)

    def test_sql_derivation_equals_python(self):
        self.migrate(self.upgrade, NEW.REVISION)
        key = derive_provider_key("t", "rk-1", "mock-payments", "acct")
        self.insert("rk-1", key)
        self.assertEqual(self.query(
            "SELECT count(*) FROM public.safeact_operations"
        ), [(1,)])

    def test_upgrade_refuses_existing_duplicate_keys(self):
        self.migrate(self.upgrade, OLD.REVISION)
        self.insert("rk-1", "shared-key")
        self.insert("rk-2", "shared-key")

        with self.assertRaisesRegex(RuntimeError, "share provider keys"):
            self.migrate(self.upgrade, NEW.REVISION)
        self.assertEqual(self.version(), [(OLD.REVISION,)])

    def test_upgrade_refuses_keys_that_are_not_derived(self):
        self.migrate(self.upgrade, OLD.REVISION)
        self.insert("rk-1", "caller-chosen")

        with self.assertRaisesRegex(RuntimeError, "not derived"):
            self.migrate(self.upgrade, NEW.REVISION)
        self.assertEqual(self.version(), [(OLD.REVISION,)])

    def test_autogenerate_never_proposes_dropping_head_objects(self):
        self.migrate(self.upgrade, "head")
        self.assertEqual(
            self.removals(lsql.autogenerate_include_object), set()
        )


if __name__ == "__main__":
    unittest.main()
