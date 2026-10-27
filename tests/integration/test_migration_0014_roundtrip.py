"""Alembic 0014 upgrade/downgrade round trip on a throwaway database."""

import os
import unittest
import warnings

import sqlalchemy as sa

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from storage import durable_lifecycle_sql as lsql
from storage.lifecycle_sql import v0013 as OLD, v0014 as NEW
from storage.schema import metadata
from tests.integration.migration_roundtrip import MigrationRoundTripCase


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class Migration0014RoundTripTests(MigrationRoundTripCase):

    def audit_columns(self):
        return [row[0] for row in self.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "AND table_name = 'safeact_audit_events' ORDER BY 1"
        )]

    def test_upgrade_downgrade_upgrade(self):
        self.migrate(self.upgrade, OLD.REVISION)
        before, columns = self.functions(), self.audit_columns()

        self.migrate(self.upgrade, NEW.REVISION)
        upgraded = self.functions()
        self.assertEqual(set(upgraded) - set(before), set(NEW.NEW_FUNCTIONS))
        self.assertEqual(set(self.audit_columns()) - set(columns), {"commit_xid"})

        self.migrate(self.downgrade, OLD.REVISION)
        self.assertEqual(self.functions(), before)
        self.assertEqual(self.audit_columns(), columns)

        self.migrate(self.upgrade, NEW.REVISION)
        self.assertEqual(self.functions(), upgraded)

    def test_existing_and_new_audit_rows_carry_their_transaction(self):
        self.migrate(self.upgrade, NEW.REVISION)
        self.assertEqual(self.query(
            "SELECT count(*) FROM safeact_audit_events WHERE commit_xid IS NULL"
        ), [(0,)])

    def test_autogenerate_never_proposes_dropping_head_objects(self):
        self.migrate(self.upgrade, "head")
        self.assertEqual(
            self.removals(lsql.autogenerate_include_object), set()
        )
        # The column on a foundation table is protected too.
        with self.engine.connect() as connection:
            diffs = compare_metadata(MigrationContext.configure(
                connection, opts={
                    "compare_type": True,
                    "include_object": lsql.autogenerate_include_object,
                },
            ), metadata)
        flat = []
        for diff in diffs:
            flat.extend(diff if isinstance(diff, list) else [diff])
        self.assertFalse([d for d in flat if d[0] == "remove_column"])


    def test_xid8_reflection_preserves_the_native_database_type(self):
        self.migrate(self.upgrade, NEW.REVISION)
        self.assertEqual(self.query("""
            SELECT format_type(atttypid, atttypmod)
            FROM pg_attribute
            WHERE attrelid = 'public.safeact_audit_events'::regclass
              AND attname = 'commit_xid' AND NOT attisdropped
        """), [("xid8",)])
        self.assertIsNot(
            self.engine.dialect.ischema_names,
            type(self.engine.dialect).ischema_names,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error", sa.exc.SAWarning)
            columns = sa.inspect(self.engine).get_columns(
                "safeact_audit_events", schema="public")
        column = next(c for c in columns if c["name"] == "commit_xid")
        self.assertNotIsInstance(column["type"], sa.types.NullType)
        self.assertEqual(
            column["type"].compile(dialect=self.engine.dialect).lower(),
            "xid8",
        )


if __name__ == "__main__":
    unittest.main()
