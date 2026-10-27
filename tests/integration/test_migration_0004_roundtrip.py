"""Alembic 0004 upgrade/downgrade round trip on a throwaway database."""

import os
import unittest

from core.connector_profiles import PROFILE_COLUMNS, payments_profile
from storage import durable_lifecycle_sql as lsql
from storage.lifecycle_sql import v0004
from tests.integration.migration_roundtrip import MigrationRoundTripCase


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class Migration0004RoundTripTests(MigrationRoundTripCase):

    def register(self, profile):
        row = profile.row()
        self.execute(
            "INSERT INTO public.safeact_connector_contracts ("
            + ", ".join(PROFILE_COLUMNS) + ") VALUES ("
            + ", ".join(f":{c}" for c in PROFILE_COLUMNS) + ")",
            row,
        )

    def test_upgrade_downgrade_upgrade_restores_0003_fence(self):
        self.migrate(self.upgrade, "0003_durable_lifecycle")
        before = self.functions()
        relations_before = self.relations()

        self.migrate(self.upgrade, v0004.REVISION)
        self.assertEqual(self.version(), [(v0004.REVISION,)])
        upgraded = self.functions()
        self.assertEqual(
            self.relations() - relations_before,
            set(v0004.NEW_TABLES),
        )
        self.assertEqual(
            set(upgraded) - set(before), set(v0004.NEW_FUNCTIONS)
        )
        self.assertIn(
            "safeact_connector_contracts",
            upgraded["safeact_begin_dispatch"][0],
        )
        # Replacing the fence keeps its owner and ACL; nothing is
        # granted to PUBLIC or anyone else.
        self.assertEqual(
            upgraded["safeact_begin_dispatch"][1:],
            before["safeact_begin_dispatch"][1:],
        )
        for name in v0004.NEW_FUNCTIONS:
            owner, acl = upgraded[name][1:]
            entries = (acl or "{}").strip("{}").split(",")
            # Only the owner holds EXECUTE; PUBLIC ("=X/...") has none.
            self.assertEqual(entries, [f"{owner}=X/{owner}"])
        self.assertEqual(self.query(
            "SELECT count(*) FROM public.safeact_connector_contracts"
        ), [(0,)])

        self.migrate(self.downgrade, "0003_durable_lifecycle")
        self.assertEqual(self.version(), [("0003_durable_lifecycle",)])
        self.assertEqual(self.functions(), before)
        self.assertEqual(self.relations(), relations_before)

        self.migrate(self.upgrade, v0004.REVISION)
        self.assertEqual(self.functions(), upgraded)

    def test_downgrade_refuses_while_contracts_are_registered(self):
        self.migrate(self.upgrade, v0004.REVISION)
        self.register(payments_profile("v-roundtrip"))

        with self.assertRaisesRegex(RuntimeError, "registered contracts"):
            self.migrate(self.downgrade, "0003_durable_lifecycle")

        self.assertEqual(self.version(), [(v0004.REVISION,)])

    def test_autogenerate_never_proposes_dropping_head_objects(self):
        self.migrate(self.upgrade, "head")

        self.assertIn(
            ("remove_table", "safeact_connector_contracts"),
            self.removals(),
        )
        self.assertEqual(
            self.removals(lsql.autogenerate_include_object), set()
        )


if __name__ == "__main__":
    unittest.main()
