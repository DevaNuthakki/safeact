"""Alembic 0009 upgrade/downgrade round trip on a throwaway database."""

import os
import unittest

from uuid import uuid4

from storage import durable_lifecycle_sql as lsql
from storage.lifecycle_sql import v0008 as OLD, v0009 as NEW
from tests.integration.migration_roundtrip import MigrationRoundTripCase


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class Migration0009RoundTripTests(MigrationRoundTripCase):

    def test_upgrade_downgrade_upgrade(self):
        self.migrate(self.upgrade, OLD.REVISION)
        before, relations = self.functions(), self.relations()

        self.migrate(self.upgrade, NEW.REVISION)
        upgraded = self.functions()
        self.assertEqual(
            self.relations() - relations,
            set(NEW.NEW_TABLES) | set(NEW.NEW_VIEWS),
        )
        self.assertEqual(set(upgraded) - set(before), set(NEW.NEW_FUNCTIONS))
        for name in NEW.NEW_FUNCTIONS:
            owner, acl = upgraded[name][1:]
            self.assertEqual(
                (acl or "{}").strip("{}").split(","), [f"{owner}=X/{owner}"]
            )

        self.migrate(self.downgrade, OLD.REVISION)
        self.assertEqual(self.functions(), before)
        self.assertEqual(self.relations(), relations)

        self.migrate(self.upgrade, NEW.REVISION)
        self.assertEqual(self.functions(), upgraded)

    def test_downgrade_refuses_while_a_workflow_exists(self):
        self.migrate(self.upgrade, NEW.REVISION)
        self.execute("""
            INSERT INTO public.safeact_workflows (
                workflow_id, tenant_id, request_key, workflow_type,
                created_by, plan
            ) VALUES (
                :id, 't', 'k', 'replacement_shipping_v1', 'a',
                '{"order": {}, "compensation": {}}'
            )
        """, {"id": uuid4()})

        with self.assertRaisesRegex(RuntimeError, "workflow history"):
            self.migrate(self.downgrade, OLD.REVISION)
        self.assertEqual(self.version(), [(NEW.REVISION,)])

    def test_autogenerate_never_proposes_dropping_head_objects(self):
        self.migrate(self.upgrade, "head")
        self.assertEqual(
            self.removals(lsql.autogenerate_include_object), set()
        )


if __name__ == "__main__":
    unittest.main()
