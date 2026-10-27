"""Durable lifecycle least-privilege and function-hardening tests.

Disposable schema and test-only roles. Proves that callers can only
EXECUTE approved functions, that the function owner holds exactly the
minimum it needs, and that search_path tricks do not reach the
SECURITY DEFINER bodies.
"""

import os
import unittest

import psycopg
from psycopg import sql

from core.lifecycle import LifecycleState as S
from storage import durable_lifecycle as L
from storage import durable_lifecycle_sql as lsql
from tests.integration.lifecycle_fixture import (
    FOUNDATION_TABLES,
    DisposableLifecycleDatabase,
    commit,
)


ALL_TABLES = FOUNDATION_TABLES + lsql.NEW_TABLES


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1"
    and os.environ.get("SAFEACT_RUN_DB_SECURITY_TESTS") == "1",
    "Disposable database security tests are disabled",
)
class LifecyclePrivilegeTests(unittest.TestCase):

    def setUp(self):
        self.db = DisposableLifecycleDatabase()
        self.addCleanup(self.db.cleanup)
        self.db.start()
        self.schema = self.db.schema
        self.admin = self.db.admin

    # -- catalog helpers ---------------------------------------------

    def relation_grants(self, role):
        return set(self.admin.execute("""
            SELECT n.nspname, c.relname, a.privilege_type
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            CROSS JOIN LATERAL aclexplode(c.relacl) a
            WHERE a.grantee = %s::regrole
        """, (role,)).fetchall())

    def column_grants(self, role):
        return set(self.admin.execute("""
            SELECT n.nspname, c.relname, t.attname, a.privilege_type
            FROM pg_attribute t
            JOIN pg_class c ON c.oid = t.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            CROSS JOIN LATERAL aclexplode(t.attacl) a
            WHERE a.grantee = %s::regrole
        """, (role,)).fetchall())

    def schema_grants(self, role):
        return set(self.admin.execute("""
            SELECT n.nspname, a.privilege_type
            FROM pg_namespace n
            CROSS JOIN LATERAL aclexplode(n.nspacl) a
            WHERE a.grantee = %s::regrole
        """, (role,)).fetchall())

    def executable(self, role):
        return {
            name for (name,) in self.admin.execute("""
                SELECT p.proname FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = %s
                  AND has_function_privilege(%s, p.oid, 'EXECUTE')
            """, (self.schema, role)).fetchall()
        }

    def role_attributes(self, role):
        return self.admin.execute("""
            SELECT rolsuper, rolinherit, rolcreaterole, rolcreatedb,
                   rolcanlogin, rolreplication, rolbypassrls,
                   (SELECT count(*) FROM pg_auth_members m
                    WHERE m.member = r.oid)
            FROM pg_roles r WHERE rolname = %s
        """, (role,)).fetchone()

    def first_column(self, table):
        return self.admin.execute("""
            SELECT a.attname FROM pg_attribute a
            WHERE a.attrelid = %s::regclass AND a.attnum = 1
        """, (f'"{self.schema}"."{table}"',)).fetchone()[0]

    def call_sql(self, name):
        types = [
            t.strip() for t in lsql.ALL_FUNCTIONS[name].split(",")
            if t.strip()
        ]
        return sql.SQL("SELECT {}.{}({})").format(
            sql.Identifier(self.schema),
            sql.Identifier(name),
            sql.SQL(", ").join(
                sql.SQL("NULL::" + t) for t in types
            ),
        )

    def assert_denied(self, connection, statement):
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            try:
                connection.execute(statement)
            finally:
                connection.rollback()

    # -- callers -----------------------------------------------------

    def test_callers_have_no_direct_table_access(self):
        statements = (
            "SELECT 1 FROM {t} LIMIT 1",
            "INSERT INTO {t} DEFAULT VALUES",
            "UPDATE {t} SET {c} = {c}",
            "DELETE FROM {t}",
            "TRUNCATE {t}",
        )

        for role in (
            self.db.worker_a, self.db.recoverer, self.db.stranger
        ):
            connection = self.db.connect(role)

            for table in ALL_TABLES:
                for statement in statements:
                    with self.subTest(
                        role=role[:20], table=table, statement=statement
                    ):
                        self.assert_denied(
                            connection,
                            sql.SQL(statement).format(
                                t=self.db.table(table),
                                c=sql.Identifier(
                                    self.first_column(table)
                                ),
                            ),
                        )

    def test_caller_privileges_are_exact(self):
        view = (self.schema, lsql.CURRENT_STATE_VIEW, "SELECT")

        expected = {
            self.db.worker_a: (
                {view},
                set(lsql.WORKER_FUNCTIONS) | set(lsql.SHARED_FUNCTIONS),
            ),
            self.db.recoverer: (
                {view},
                set(lsql.RECOVERY_FUNCTIONS)
                | set(lsql.SHARED_FUNCTIONS),
            ),
            self.db.stranger: (set(), set()),
        }

        for role, (relations, functions) in expected.items():
            with self.subTest(role=role[:20]):
                self.assertEqual(self.relation_grants(role), relations)
                self.assertEqual(self.column_grants(role), set())
                self.assertEqual(self.executable(role), functions)
                self.assertEqual(
                    self.schema_grants(role),
                    set() if role == self.db.stranger
                    else {(self.schema, "USAGE")},
                )
                self.assertEqual(
                    self.role_attributes(role),
                    (False, False, False, False, True, False, False, 0),
                )

    def test_internal_and_cross_family_functions_are_denied(self):
        worker = self.db.connect(self.db.worker_a)
        recoverer = self.db.connect(self.db.recoverer)

        cases = [
            (worker, name) for name in (
                *lsql.INTERNAL_FUNCTIONS, *lsql.RECOVERY_FUNCTIONS,
            )
        ] + [
            (recoverer, name) for name in (
                *lsql.INTERNAL_FUNCTIONS, *lsql.WORKER_FUNCTIONS,
            )
        ]

        for connection, name in cases:
            with self.subTest(user=connection.info.user[:20], name=name):
                self.assert_denied(connection, self.call_sql(name))

    def test_public_cannot_execute_any_function(self):
        # Give the stranger schema USAGE so only function ACLs remain.
        self.admin.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            sql.Identifier(self.schema), sql.Identifier(self.db.stranger)
        ))
        stranger = self.db.connect(self.db.stranger)

        for name in lsql.ALL_FUNCTIONS:
            with self.subTest(name=name):
                self.assert_denied(stranger, self.call_sql(name))

        rows = self.admin.execute("""
            SELECT p.proname, p.proacl IS NULL,
                   EXISTS (SELECT 1 FROM aclexplode(p.proacl) a
                           WHERE a.grantee = 0)
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = %s
        """, (self.schema,)).fetchall()

        self.assertEqual(len(rows), len(lsql.ALL_FUNCTIONS))
        for name, default_acl, public in rows:
            with self.subTest(name=name):
                self.assertFalse(default_acl)
                self.assertFalse(public)

    def test_functions_pin_search_path_and_owner(self):
        rows = self.admin.execute("""
            SELECT p.proname, p.prosecdef, p.proconfig,
                   pg_get_userbyid(p.proowner)
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = %s
        """, (self.schema,)).fetchall()

        self.assertEqual(
            {row[0] for row in rows}, set(lsql.ALL_FUNCTIONS)
        )
        for name, definer, config, owner in rows:
            with self.subTest(name=name):
                self.assertEqual(
                    definer, name in lsql.DEFINER_FUNCTIONS
                )
                self.assertEqual(
                    config, ["search_path=pg_catalog, pg_temp"]
                )
                self.assertEqual(owner, self.db.owner)

    def test_temp_tables_cannot_shadow_lifecycle_objects(self):
        operation_id = self.db.admit()
        worker = self.db.connect(self.db.worker_a)

        # Classic SECURITY DEFINER attack: shadow tables in pg_temp
        # and put pg_temp first on the caller's search_path.
        for table in (
            "safeact_operations",
            "safeact_operation_transitions",
            "safeact_legal_transitions",
            "safeact_outbox_events",
        ):
            worker.execute(sql.SQL(
                "CREATE TEMP TABLE {} (operation_id uuid, seq integer, "
                "to_state text, from_state text)"
            ).format(sql.Identifier(table)))

        worker.execute(sql.SQL(
            "INSERT INTO pg_temp.safeact_operation_transitions "
            "VALUES (%s, 7, 'RETRY_ELIGIBLE', 'VERIFYING')"
        ), (operation_id,))
        worker.execute(sql.SQL("SET search_path = pg_temp, {}").format(
            sql.Identifier(self.schema)
        ))
        worker.commit()

        held = commit(worker, lambda: L.claim(
            worker, self.schema, operation_id, lease_seconds=60,
        ))

        self.assertEqual(held.seq, 1)
        self.assertEqual(self.db.count(
            "safeact_operation_transitions", operation_id=operation_id
        ), 1)
        self.assertEqual(
            commit(worker, lambda: L.current_state(
                worker, self.schema, operation_id
            )).state,
            S.CLAIMED,
        )
        self.assertEqual(worker.execute(
            "SELECT count(*) FROM pg_temp.safeact_operation_transitions"
        ).fetchone()[0], 1)

    # -- function owner ----------------------------------------------

    def test_function_owner_privileges(self):
        owner = self.db.owner

        expected_relations = {
            (self.schema, table, privilege)
            for table, privileges in lsql.OWNER_TABLE_PRIVILEGES.items()
            for privilege in privileges
        } | {(self.schema, lsql.CURRENT_STATE_VIEW, "SELECT")}
        expected_columns = {
            (self.schema, table, column, privilege)
            for (table, column), privileges
            in lsql.OWNER_COLUMN_PRIVILEGES.items()
            for privilege in privileges
        }

        self.assertEqual(self.relation_grants(owner), expected_relations)
        self.assertEqual(self.column_grants(owner), expected_columns)
        self.assertEqual(
            self.schema_grants(owner), {(self.schema, "USAGE")}
        )
        self.assertEqual(
            self.role_attributes(owner),
            (False, False, False, False, False, False, False, 0),
        )

        # No DELETE, TRUNCATE, REFERENCES or TRIGGER anywhere. UPDATE only
        # on the current-state projection (0017), whose guard trigger
        # accepts nothing but the next transition already in the log, and
        # on the provider health row (0018): mutable liveness state (token
        # bucket, breaker) that no safety rule depends on.
        self.assertFalse({
            privilege for _, _, privilege in expected_relations
        } & {"DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"})
        self.assertEqual({
            table for _, table, privilege in expected_relations
            if privilege == "UPDATE"
        }, {lsql.PROJECTION_TABLE, "safeact_connector_health"})

        owned = set(self.admin.execute("""
            SELECT c.relname, c.relkind FROM pg_class c
            WHERE c.relowner = %s::regrole
        """, (owner,)).fetchall())
        self.assertEqual(owned, {(lsql.CURRENT_STATE_VIEW, "v")})

    def test_owner_column_update_is_still_blocked_by_write_guard(self):
        self.db.admit()

        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.admin.transaction():
                self.admin.execute(sql.SQL("SET LOCAL ROLE {}").format(
                    sql.Identifier(self.db.owner)
                ))
                self.admin.execute(sql.SQL(
                    "UPDATE {} SET operation_id = operation_id"
                ).format(self.db.table("safeact_operations")))

        for statement in (
            "UPDATE {} SET tenant_id = tenant_id",
            "DELETE FROM {}",
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(
                    psycopg.errors.InsufficientPrivilege
                ):
                    with self.admin.transaction():
                        self.admin.execute(
                            sql.SQL("SET LOCAL ROLE {}").format(
                                sql.Identifier(self.db.owner)
                            )
                        )
                        self.admin.execute(sql.SQL(statement).format(
                            self.db.table("safeact_operations")
                        ))

        self.assertEqual(self.db.count("safeact_operations"), 1)


if __name__ == "__main__":
    unittest.main()
