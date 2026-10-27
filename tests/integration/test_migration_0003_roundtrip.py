"""Alembic 0003 upgrade/downgrade round trip on a throwaway database.

Creates a ``safeact_it_<hex32>`` database on the local dev server,
migrates it, and drops it afterwards. The dev database is untouched.
"""

import os
import re
import unittest

from pathlib import Path
from uuid import uuid4

import psycopg
import sqlalchemy as sa

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from psycopg import sql
from sqlalchemy.pool import NullPool

from storage.lifecycle_sql import v0003 as lsql
from storage.database import local_database_url
from storage.schema import metadata
from tests.integration.lifecycle_fixture import FOUNDATION_TABLES
from tests.integration.test_postgres_smoke import database_settings


ROOT = Path(__file__).resolve().parents[2]
NEW_INDEX = "ix_safeact_operations_created_at"


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class Migration0003RoundTripTests(unittest.TestCase):

    def setUp(self):
        self.name = "safeact_it_" + uuid4().hex
        if not re.fullmatch(r"safeact_it_[0-9a-f]{32}", self.name):
            raise RuntimeError("STOP: invalid disposable database")

        settings = database_settings()
        if settings["host"] != "127.0.0.1":
            raise RuntimeError("STOP: local database required")

        self.admin = psycopg.connect(**settings, autocommit=True)
        self.addCleanup(self.admin.close)

        self.admin.execute(sql.SQL("CREATE DATABASE {}").format(
            sql.Identifier(self.name)
        ))
        self.addCleanup(self.drop_database)

        self.engine = sa.create_engine(
            local_database_url().set(database=self.name),
            poolclass=NullPool,
            connect_args={"connect_timeout": 5},
        )
        self.addCleanup(self.engine.dispose)

        self.config = Config(str(ROOT / "alembic.ini"))
        self.config.set_main_option(
            "script_location", str(ROOT / "deploy" / "migrations")
        )

    def drop_database(self):
        self.engine.dispose()
        self.admin.execute(sql.SQL(
            "DROP DATABASE IF EXISTS {} WITH (FORCE)"
        ).format(sql.Identifier(self.name)))

    # -- helpers -------------------------------------------------------

    def migrate(self, action, revision):
        with self.engine.begin() as connection:
            self.config.attributes["connection"] = connection
            try:
                action(self.config, revision)
            finally:
                self.config.attributes.pop("connection", None)

    def query(self, statement, params=None):
        with self.engine.connect() as connection:
            return connection.execute(
                sa.text(statement), params or {}
            ).fetchall()

    def version(self):
        return self.query("SELECT version_num FROM alembic_version")

    def snapshot(self):
        tables = {"tables": list(FOUNDATION_TABLES)}
        relations = (
            "(SELECT oid FROM pg_class WHERE relnamespace = "
            "'public'::regnamespace AND relname = ANY(:tables))"
        )

        return {
            "constraints": set(self.query(
                "SELECT conrelid::regclass::text, conname, "
                "pg_get_constraintdef(oid) FROM pg_constraint "
                f"WHERE conrelid IN {relations}", tables,
            )),
            "triggers": set(self.query(
                "SELECT tgrelid::regclass::text, tgname, "
                "pg_get_triggerdef(oid) FROM pg_trigger "
                f"WHERE NOT tgisinternal AND tgrelid IN {relations}",
                tables,
            )),
            "columns": set(self.query(
                "SELECT table_name, column_name, data_type, "
                "is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                "AND table_name = ANY(:tables)", tables,
            )),
            "indexes": set(self.query(
                "SELECT tablename, indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' "
                "AND tablename = ANY(:tables)", tables,
            )),
        }

    def lifecycle_objects(self):
        names = set(lsql.NEW_TABLES) | {lsql.CURRENT_STATE_VIEW}
        relations = {
            name for (name,) in self.query(
                "SELECT relname FROM pg_class WHERE relnamespace = "
                "'public'::regnamespace AND relname = ANY(:names)",
                {"names": list(names)},
            )
        }
        functions = {
            name for (name,) in self.query(
                "SELECT proname FROM pg_proc WHERE pronamespace = "
                "'public'::regnamespace AND proname = ANY(:names)",
                {"names": list(lsql.ALL_FUNCTIONS)},
            )
        }
        return relations, functions

    # -- tests -----------------------------------------------------------

    def test_upgrade_downgrade_upgrade_preserves_foundation(self):
        self.migrate(command.upgrade, "0002_canonical_payload")
        base = self.snapshot()
        self.assertEqual(self.lifecycle_objects(), (set(), set()))

        self.migrate(command.upgrade, "0003_durable_lifecycle")
        upgraded = self.snapshot()

        self.assertEqual(self.version(), [("0003_durable_lifecycle",)])
        for part in ("constraints", "triggers", "columns"):
            with self.subTest(part=part):
                self.assertEqual(upgraded[part], base[part])

        added = upgraded["indexes"] - base["indexes"]
        self.assertTrue(base["indexes"] <= upgraded["indexes"])
        self.assertEqual({index for _, index, _ in added}, {NEW_INDEX})

        relations, functions = self.lifecycle_objects()
        self.assertEqual(
            relations,
            set(lsql.NEW_TABLES) | {lsql.CURRENT_STATE_VIEW},
        )
        self.assertEqual(functions, set(lsql.ALL_FUNCTIONS))

        # No role or PUBLIC may execute anything after migration.
        grants = self.query(
            "SELECT p.proname, p.proacl IS NULL, EXISTS ("
            "SELECT 1 FROM aclexplode(p.proacl) a "
            "WHERE a.grantee <> p.proowner) "
            "FROM pg_proc p WHERE p.pronamespace = "
            "'public'::regnamespace AND p.proname = ANY(:names)",
            {"names": list(lsql.ALL_FUNCTIONS)},
        )
        for name, default_acl, granted in grants:
            with self.subTest(function=name):
                self.assertFalse(default_acl)
                self.assertFalse(granted)

        self.migrate(command.downgrade, "0002_canonical_payload")

        self.assertEqual(self.version(), [("0002_canonical_payload",)])
        self.assertEqual(self.snapshot(), base)
        self.assertEqual(self.lifecycle_objects(), (set(), set()))

        self.migrate(command.upgrade, "0003_durable_lifecycle")

        self.assertEqual(self.version(), [("0003_durable_lifecycle",)])
        self.assertEqual(self.snapshot(), upgraded)

    def removals(self, include_object=None):
        options = {"compare_type": True}
        if include_object is not None:
            options["include_object"] = include_object

        with self.engine.connect() as connection:
            diffs = compare_metadata(
                MigrationContext.configure(connection, opts=options),
                metadata,
            )

        flat = []
        for diff in diffs:
            flat.extend(diff if isinstance(diff, list) else [diff])

        return {
            (diff[0], diff[1].name)
            for diff in flat
            if diff[0] in ("remove_table", "remove_index")
        }

    def test_autogenerate_never_proposes_dropping_0003_objects(self):
        self.migrate(command.upgrade, "0003_durable_lifecycle")

        unguarded = self.removals()
        guarded = self.removals(lsql.autogenerate_include_object)

        lifecycle = {
            ("remove_table", name) for name in lsql.NEW_TABLES
        } | {
            ("remove_index", name) for name in lsql.LIFECYCLE_INDEXES
        }

        # Without the guard autogenerate would drop them.
        self.assertTrue(
            {("remove_table", name) for name in lsql.NEW_TABLES}
            <= unguarded
        )
        self.assertIn(
            ("remove_index", "ix_safeact_operations_created_at"),
            unguarded,
        )
        self.assertFalse(guarded & lifecycle)
        self.assertEqual(guarded, set())

    def test_downgrade_refuses_while_history_exists(self):
        self.migrate(command.upgrade, "0003_durable_lifecycle")

        operation_id = uuid4()
        with self.engine.begin() as connection:
            connection.execute(sa.text("""
                INSERT INTO public.safeact_operations (
                    operation_id, tenant_id, request_key, action_type,
                    resource_id, provider_account_id, connector_id,
                    contract_version, payload_sha256, payload_canonical
                ) VALUES (
                    :id, 't', 'k', 'a', 'r', 'p', 'c', 'v1',
                    encode(sha256(convert_to('{}', 'UTF8')), 'hex'), '{}'
                )
            """), {"id": operation_id})
            connection.execute(sa.text("""
                INSERT INTO public.safeact_operation_transitions (
                    operation_id, seq, from_state, to_state, claim_id,
                    actor, lease_expires_at, reason
                ) VALUES (
                    :id, 1, 'RECORDED', 'CLAIMED', gen_random_uuid(),
                    'test', now(), 'history'
                )
            """), {"id": operation_id})

        with self.assertRaisesRegex(RuntimeError, "execution history"):
            self.migrate(command.downgrade, "0002_canonical_payload")

        self.assertEqual(self.version(), [("0003_durable_lifecycle",)])
        self.assertEqual(self.query(
            "SELECT count(*) FROM public.safeact_operation_transitions"
        ), [(1,)])


if __name__ == "__main__":
    unittest.main()
