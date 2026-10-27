"""The demo bootstrap rebuilds an environment idempotently.

Runs the same ``rebuild`` the ``deploy.demo.bootstrap`` command uses,
against a throwaway ``safeact_it_<hex>`` environment so the test never
touches a developer's live ``safeact_demo``. Requires
SAFEACT_RUN_DB_INTEGRATION=1 and SAFEACT_RUN_DISPOSABLE_DB_TESTS=1.
"""

import os
import unittest
import uuid

import psycopg
import sqlalchemy as sa
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy.pool import NullPool

from api.app import create_app
from api.settings import Settings
from core.connector_profiles import MOCK_PROFILES
from deploy.demo.bootstrap import (
    DEMO_ACCOUNTS, Environment, rebuild, teardown_statements,
)
from storage import durable_lifecycle_sql as lsql
from storage.database import local_admin_settings, local_database_url
from storage.submission import IntentSubmission, submit_intent


ENABLED = (
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1"
)

CATALOG = """
SELECT 'table', c.relname, NULL FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = %(s)s AND c.relkind IN ('r', 'v', 'i')
UNION ALL
SELECT 'column', c.relname || '.' || a.attname,
       format_type(a.atttypid, a.atttypmod) || coalesce(' ' || a.attnotnull::text, '')
  FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = %(s)s AND a.attnum > 0 AND NOT a.attisdropped
UNION ALL
SELECT 'constraint', conrelid::regclass::text || ':' || conname,
       pg_get_constraintdef(oid)
  FROM pg_constraint
 WHERE connamespace = %(s)s::regnamespace
UNION ALL
SELECT 'function', p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',
       md5(p.prosrc) || ' ' || p.prosecdef::text || ' ' || coalesce(array_to_string(p.proacl, ','), '')
  FROM pg_proc p
 WHERE p.pronamespace = %(s)s::regnamespace
UNION ALL
SELECT 'trigger', tgrelid::regclass::text || ':' || tgname, NULL
  FROM pg_trigger
 WHERE NOT tgisinternal
   AND tgrelid IN (SELECT oid FROM pg_class WHERE relnamespace = %(s)s::regnamespace)
UNION ALL
SELECT 'acl', c.relname, coalesce(array_to_string(c.relacl, ','), '')
  FROM pg_class c
 WHERE c.relnamespace = %(s)s::regnamespace
"""


@unittest.skipUnless(ENABLED, "PostgreSQL integration tests are opt-in")
class DemoBootstrapTests(unittest.TestCase):

    def setUp(self):
        self.admin = psycopg.connect(**local_admin_settings(), autocommit=True)
        self.env = Environment.disposable(uuid.uuid4().hex)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        try:
            self.admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE usename = ANY(%s)", (list(self.env.roles),),
            )
            with self.admin.transaction():
                for statement in teardown_statements(self.admin, self.env):
                    self.admin.execute(statement)
        finally:
            self.admin.close()

    def catalog(self):
        rows = self.admin.execute(CATALOG, {"s": self.env.schema}).fetchall()
        # Role passwords differ per run; nothing else may.
        return sorted(rows)

    def connect(self, role, password):
        settings = local_admin_settings()
        settings.update(user=role, password=password)
        return psycopg.connect(**settings, autocommit=True)

    def contracts(self):
        return self.admin.execute(sql.SQL(
            "SELECT connector_id, version FROM {}.safeact_connector_contracts "
            "ORDER BY 1, 2"
        ).format(sql.Identifier(self.env.schema))).fetchall()

    def test_rebuild_twice_gives_the_same_catalog_and_contracts(self):
        first_passwords, first_accounts = rebuild(
            self.admin, self.env, accounts=DEMO_ACCOUNTS
        )
        first = self.catalog()
        first_contracts = self.contracts()

        second_passwords, second_accounts = rebuild(
            self.admin, self.env, accounts=DEMO_ACCOUNTS
        )
        self.assertEqual(self.catalog(), first)
        self.assertEqual(self.contracts(), first_contracts)
        self.assertEqual(
            first_contracts,
            sorted((p.connector_id, p.version) for p in MOCK_PROFILES),
        )
        self.assertNotEqual(first_passwords, second_passwords)
        self.assertEqual(set(first_accounts), set(second_accounts))
        self.assertNotEqual(first_accounts, second_accounts)
        self.assertEqual(
            self.admin.execute(sql.SQL(
                "SELECT count(*) FROM {}.safeact_principals"
            ).format(sql.Identifier(self.env.schema))).fetchone()[0],
            len(DEMO_ACCOUNTS),
        )

        # Old role passwords stop working; new ones log in.
        for role in self.env.login_roles:
            with self.assertRaises(psycopg.OperationalError):
                self.connect(role, first_passwords[role]).close()
            with self.connect(role, second_passwords[role]) as connection:
                self.assertEqual(
                    connection.execute("SELECT session_user").fetchone()[0],
                    role,
                )

    def test_dirty_environment_is_rebuilt_clean(self):
        rebuild(self.admin, self.env)
        table = sql.SQL("{}.safeact_operations").format(
            sql.Identifier(self.env.schema)
        )
        engine = sa.create_engine(
            local_database_url(), poolclass=NullPool,
            connect_args={"options": f"-c search_path={self.env.schema},public"},
        )
        try:
            submit_intent(engine, IntentSubmission(
                tenant_id="synthetic-tenant", actor_id="synthetic-actor",
                request_key="dirty", action_type="refund_payment",
                resource_id="synthetic-charge",
                provider_account_id="mock-payments-account",
                connector_id="mock-payments", contract_version="v1",
                payload={"charge_id": "synthetic-charge", "amount_minor": 1},
            ))
        finally:
            engine.dispose()
        self.assertEqual(
            self.admin.execute(sql.SQL("SELECT count(*) FROM {}").format(table))
            .fetchone()[0], 1,
        )

        # A session held by a demo role does not block the rebuild.
        passwords, _ = rebuild(self.admin, self.env)
        held = self.connect(self.env.worker, passwords[self.env.worker])
        rebuild(self.admin, self.env)
        self.assertTrue(held.closed or held.broken or self._dead(held))

        self.assertEqual(
            self.admin.execute(sql.SQL("SELECT count(*) FROM {}").format(table))
            .fetchone()[0], 0,
        )

    @staticmethod
    def _dead(connection):
        try:
            connection.execute("SELECT 1")
        except psycopg.Error:
            return True
        return False

    def test_roles_are_unprivileged_and_api_has_no_table_access(self):
        passwords, _ = rebuild(self.admin, self.env)
        flags = self.admin.execute(
            "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
            "rolreplication, rolbypassrls, rolinherit FROM pg_roles "
            "WHERE rolname = ANY(%s)", (list(self.env.roles),),
        ).fetchall()
        self.assertEqual(len(flags), len(self.env.roles))
        for name, login, *privileged in flags:
            self.assertEqual(login, name != self.env.owner)
            self.assertEqual(privileged, [False] * 6, name)

        with self.connect(self.env.api, passwords[self.env.api]) as api:
            for table in ("safeact_operations", "safeact_audit_events"):
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    api.execute(sql.SQL("SELECT 1 FROM {}.{}").format(
                        sql.Identifier(self.env.schema), sql.Identifier(table)
                    ))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                api.execute(sql.SQL("SELECT * FROM {}.safeact_accounting()").format(
                    sql.Identifier(self.env.schema)
                ))

    def test_rebuild_never_touches_public(self):
        before = self.admin.execute(
            "SELECT count(*) FROM public.safeact_connector_contracts"
        ).fetchone()[0]
        rebuild(self.admin, self.env)
        self.assertEqual(before, 0)
        self.assertEqual(
            self.admin.execute(
                "SELECT count(*) FROM public.safeact_connector_contracts"
            ).fetchone()[0], 0,
        )
        self.assertEqual(
            self.admin.execute(
                "SELECT version_num FROM public.alembic_version"
            ).fetchone()[0], lsql.HEAD_REVISION,
        )

    def test_readyz_against_the_api_role(self):
        passwords, _ = rebuild(self.admin, self.env)
        admin = local_admin_settings()
        settings = Settings(
            schema=self.env.schema, role=self.env.api,
            password=passwords[self.env.api],
            host=admin["host"], port=admin["port"], dbname=admin["dbname"],
        )
        client = TestClient(create_app(settings))
        self.assertEqual(client.get("/readyz").status_code, 200)

        bad = Settings(
            schema=self.env.schema, role=self.env.api, password="wrong",
            host=admin["host"], port=admin["port"], dbname=admin["dbname"],
        )
        self.assertEqual(
            TestClient(create_app(bad)).get("/readyz").status_code, 503
        )


@unittest.skipUnless(ENABLED, "PostgreSQL integration tests are opt-in")
class DemoEnvironmentNameTests(unittest.TestCase):

    def test_public_is_refused(self):
        with self.assertRaises(ValueError):
            Environment(
                schema="public", owner="safeact_x_owner", api="safeact_x_api",
                worker="safeact_x_worker", recoverer="safeact_x_rc",
                advancer="safeact_x_av", mcp="safeact_x_mcp",
            )

    def test_demo_names(self):
        env = Environment.demo()
        self.assertEqual(env.schema, "safeact_demo")
        self.assertTrue(all(r.startswith("safeact_demo_") for r in env.roles))


if __name__ == "__main__":
    unittest.main()
