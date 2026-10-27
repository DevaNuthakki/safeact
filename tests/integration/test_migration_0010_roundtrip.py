"""Alembic 0010 upgrade/downgrade round trip on a throwaway database."""

import os
import unittest

from uuid import uuid4

from storage import durable_lifecycle_sql as lsql
from storage.lifecycle_sql import v0009 as OLD, v0010 as NEW
from tests.integration.migration_roundtrip import MigrationRoundTripCase


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class Migration0010RoundTripTests(MigrationRoundTripCase):

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

    def test_downgrade_refuses_while_a_compensation_exists(self):
        self.migrate(self.upgrade, NEW.REVISION)
        workflow, charge, refund = uuid4(), uuid4(), uuid4()
        self.execute("""
            INSERT INTO public.safeact_workflows (
                workflow_id, tenant_id, request_key, workflow_type,
                created_by, plan
            ) VALUES (
                :w, 't', 'k', 'replacement_shipping_v1', 'a',
                '{"order": {}, "compensation": {}}'
            )
        """, {"w": workflow})
        for operation_id, key in ((charge, "c"), (refund, "r")):
            self.execute("""
                INSERT INTO public.safeact_operations (
                    operation_id, tenant_id, request_key, action_type,
                    resource_id, provider_account_id, connector_id,
                    contract_version, payload_sha256, payload_canonical
                ) VALUES (
                    :id, 't', :k, 'a', 'r', 'p', 'mock-crm', 'v1',
                    encode(sha256(convert_to('{}', 'UTF8')), 'hex'), '{}'
                )
            """, {"id": operation_id, "k": key})
        self.execute("""
            INSERT INTO public.safeact_workflow_steps
                (workflow_id, step, operation_id, compensates_operation_id)
            VALUES (:w, 'charge', :c, NULL), (:w, 'compensation', :r, :c)
        """, {"w": workflow, "c": charge, "r": refund})

        with self.assertRaisesRegex(RuntimeError, "Compensations exist"):
            self.migrate(self.downgrade, OLD.REVISION)
        self.assertEqual(self.version(), [(NEW.REVISION,)])

    def test_autogenerate_never_proposes_dropping_head_objects(self):
        self.migrate(self.upgrade, "head")
        self.assertEqual(
            self.removals(lsql.autogenerate_include_object), set()
        )


if __name__ == "__main__":
    unittest.main()
