"""Read-only effective-privilege test for SafeAct's local role."""

import os
import unittest

import psycopg

from storage import durable_lifecycle_sql as lsql
from tests.integration.test_postgres_smoke import database_settings


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DB_SECURITY_TESTS") == "1",
    "Explicit local database-security opt-in required",
)
class AdmissionRolePolicyTests(unittest.TestCase):

    def test_restricted_role_has_exact_effective_privileges(self):
        role_name = "safeact_admission_writer"
        admission_tables = {
            "safeact_operations",
            "safeact_audit_events",
            "safeact_outbox_events",
        }
        all_tables = admission_tables | {"safeact_attempts"}

        with psycopg.connect(**database_settings()) as db:
            db.execute("SET TRANSACTION READ ONLY")

            identity = db.execute("""
                SELECT current_database(), current_user
            """).fetchone()

            self.assertEqual(
                identity, ("safeact_dev", "safeact_dev")
            )

            attributes = db.execute("""
                SELECT
                    rolcanlogin, rolsuper, rolcreatedb,
                    rolcreaterole, rolreplication,
                    rolbypassrls, rolinherit
                FROM pg_roles
                WHERE rolname = %s
            """, (role_name,)).fetchone()

            self.assertEqual(
                attributes, (False,) * 7
            )

            self.assertEqual(
                db.execute("""
                    SELECT version_num
                    FROM public.alembic_version
                """).fetchone()[0],
                lsql.HEAD_REVISION,
            )

            self.assertTrue(
                db.execute("""
                    SELECT has_schema_privilege(
                        %s, 'public', 'USAGE'
                    )
                """, (role_name,)).fetchone()[0]
            )

            self.assertFalse(
                db.execute("""
                    SELECT has_schema_privilege(
                        %s, 'public', 'CREATE'
                    )
                """, (role_name,)).fetchone()[0]
            )

            self.assertFalse(
                db.execute("""
                    SELECT has_database_privilege(
                        %s, current_database(), 'CREATE'
                    )
                """, (role_name,)).fetchone()[0]
            )

            for table in all_tables:
                for privilege in (
                    "SELECT", "INSERT", "UPDATE",
                    "DELETE", "TRUNCATE",
                    "REFERENCES", "TRIGGER",
                ):
                    expected = (
                        table in admission_tables
                        and privilege in ("SELECT", "INSERT")
                    )

                    actual = db.execute("""
                        SELECT has_table_privilege(
                            %s, %s, %s
                        )
                    """, (
                        role_name,
                        f"public.{table}",
                        privilege,
                    )).fetchone()[0]

                    with self.subTest(
                        table=table, privilege=privilege
                    ):
                        self.assertIs(actual, expected)

            for privilege in ("USAGE", "SELECT", "UPDATE"):
                actual = db.execute("""
                    SELECT has_sequence_privilege(
                        %s,
                        'public.safeact_audit_events_audit_id_seq',
                        %s
                    )
                """, (
                    role_name, privilege
                )).fetchone()[0]

                self.assertFalse(actual)

            # Explicitly document the remaining tenant
            # boundary. RLS absence is not an isolation pass.
            rls = db.execute("""
                SELECT c.relname, c.relrowsecurity
                FROM pg_class c
                JOIN pg_namespace n
                  ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname = ANY(%s)
            """, (sorted(all_tables),)).fetchall()

            self.assertEqual(
                {name for name, _ in rls},
                all_tables,
            )

            self.assertTrue(
                all(not enabled for _, enabled in rls)
            )

    def test_admission_role_has_nothing_on_lifecycle_objects(self):
        role_name = "safeact_admission_writer"
        relations = (*lsql.NEW_TABLES, lsql.CURRENT_STATE_VIEW)

        with psycopg.connect(**database_settings()) as db:
            db.execute("SET TRANSACTION READ ONLY")

            present = {
                name for (name,) in db.execute("""
                    SELECT relname FROM pg_class
                    WHERE relnamespace = 'public'::regnamespace
                      AND relname = ANY(%s)
                """, (list(relations),)).fetchall()
            }
            self.assertEqual(present, set(relations))

            for relation in relations:
                for privilege in (
                    "SELECT", "INSERT", "UPDATE", "DELETE",
                    "TRUNCATE", "REFERENCES", "TRIGGER",
                ):
                    with self.subTest(
                        relation=relation, privilege=privilege
                    ):
                        self.assertFalse(db.execute(
                            "SELECT has_table_privilege(%s, %s, %s)",
                            (role_name, f"public.{relation}", privilege),
                        ).fetchone()[0])

            for name, arguments in lsql.ALL_FUNCTIONS.items():
                with self.subTest(function=name):
                    self.assertFalse(db.execute(
                        "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                        (role_name, f"public.{name}({arguments})"),
                    ).fetchone()[0])

            # Column-level grants are not covered by has_table_privilege.
            self.assertEqual(db.execute("""
                SELECT count(*) FROM pg_attribute t
                JOIN pg_class c ON c.oid = t.attrelid
                CROSS JOIN LATERAL aclexplode(t.attacl) a
                WHERE c.relnamespace = 'public'::regnamespace
                  AND a.grantee = %s::regrole
            """, (role_name,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
