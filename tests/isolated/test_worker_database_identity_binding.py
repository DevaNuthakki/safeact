"""Disposable, deny-only PostgreSQL worker identity tests.

No production database or external provider is contacted.
Per-worker PostgreSQL credentials are a test-only mechanism.
JWT-to-database-session binding remains an unresolved gate.
"""

import os
import secrets
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql

from tests.isolated.test_postgresql_identity_isolation import (
    ADMIN,
    OWNER,
    ADMISSION,
    STRANGER,
    DATABASE,
    DisposableCluster,
)

WORKER_A = "sa_iso_worker_a"
WORKER_B = "sa_iso_worker_b"
WORKERS = (WORKER_A, WORKER_B)

REVIEW = (
    "SELECT scope_matches, provider_dispatch_authorized, "
    "retry_authorized, database_execution_authorized "
    "FROM sa_worker.review_scope(%s, %s, %s, %s)"
)


class WorkerDatabaseIdentityBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get(
            "SAFEACT_RUN_ISOLATED_CLUSTER_TESTS"
        ) != "1":
            raise unittest.SkipTest(
                "Explicit disposable-cluster opt-in required"
            )

        cls.cluster = DisposableCluster()
        cls.addClassCleanup(cls.cluster.cleanup)
        cls.cluster.start()

        cls.passwords = {
            role: secrets.token_urlsafe(40)
            for role in WORKERS
        }

        cls.operations = {
            WORKER_A: uuid4(),
            WORKER_B: uuid4(),
        }

        cls.additional_operations = set()

        with cls.admin() as admin:
            for role in WORKERS:
                admin.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER "
                        "NOCREATEDB NOCREATEROLE NOINHERIT "
                        "NOREPLICATION NOBYPASSRLS "
                        "PASSWORD {}"
                    ).format(
                        sql.Identifier(role),
                        sql.Literal(cls.passwords[role]),
                    )
                )

            admin.execute(
                sql.SQL(
                    "GRANT CONNECT ON DATABASE {} TO {}, {}"
                ).format(
                    sql.Identifier(DATABASE),
                    sql.Identifier(WORKER_A),
                    sql.Identifier(WORKER_B),
                )
            )

            admin.execute(
                sql.SQL(
                    "CREATE SCHEMA sa_worker AUTHORIZATION {}"
                ).format(sql.Identifier(OWNER))
            )

            admin.execute(
                """
                CREATE TABLE sa_worker.assignments (
                    operation_id uuid PRIMARY KEY,
                    login_role text NOT NULL,
                    tenant_id text NOT NULL,
                    connector_id text NOT NULL,
                    provider_account_id text NOT NULL,
                    enabled boolean NOT NULL
                )
                """
            )

            admin.execute(
                sql.SQL(
                    "ALTER TABLE sa_worker.assignments OWNER TO {}"
                ).format(sql.Identifier(OWNER))
            )

            admin.execute(
                sql.SQL(
                    "GRANT USAGE ON SCHEMA sa_worker "
                    "TO {}, {}, {}, {}"
                ).format(
                    sql.Identifier(WORKER_A),
                    sql.Identifier(WORKER_B),
                    sql.Identifier(ADMISSION),
                    sql.Identifier(STRANGER),
                )
            )

            admin.execute(
                """
                CREATE FUNCTION sa_worker.review_scope(
                    p_operation uuid,
                    p_tenant text,
                    p_connector text,
                    p_account text
                )
                RETURNS TABLE (
                    scope_matches boolean,
                    provider_dispatch_authorized boolean,
                    retry_authorized boolean,
                    database_execution_authorized boolean
                )
                LANGUAGE plpgsql
                VOLATILE
                SECURITY DEFINER
                SET search_path = pg_catalog
                AS $function$
                DECLARE
                    v_match boolean;
                BEGIN
                    SELECT TRUE INTO v_match
                    FROM sa_worker.assignments
                    WHERE login_role = session_user
                      AND operation_id = p_operation
                      AND tenant_id = p_tenant
                      AND connector_id = p_connector
                      AND provider_account_id = p_account
                      AND enabled IS TRUE
                    FOR SHARE;

                    RETURN QUERY SELECT
                        COALESCE(v_match, FALSE),
                        FALSE,
                        FALSE,
                        FALSE;
                END;
                $function$
                """
            )

            admin.execute(
                sql.SQL(
                    "ALTER FUNCTION "
                    "sa_worker.review_scope(uuid,text,text,text) "
                    "OWNER TO {}"
                ).format(sql.Identifier(OWNER))
            )

            admin.execute(
                "REVOKE ALL ON FUNCTION "
                "sa_worker.review_scope(uuid,text,text,text) "
                "FROM PUBLIC"
            )

            admin.execute(
                sql.SQL(
                    "GRANT EXECUTE ON FUNCTION "
                    "sa_worker.review_scope(uuid,text,text,text) "
                    "TO {}, {}"
                ).format(
                    sql.Identifier(WORKER_A),
                    sql.Identifier(WORKER_B),
                )
            )

            for role in WORKERS:
                admin.execute(
                    """
                    INSERT INTO sa_worker.assignments (
                        operation_id, login_role, tenant_id,
                        connector_id, provider_account_id,
                        enabled
                    )
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                    """,
                    (
                        cls.operations[role],
                        role,
                        "tenant-one",
                        "connector-one",
                        "provider-one",
                    ),
                )

        print(
            "PASS: Two independent worker logins provisioned",
            flush=True,
        )
        print(
            "PASS: Protected assignments and deny-only "
            "SECURITY DEFINER function created",
            flush=True,
        )

    @classmethod
    def admin(cls):
        return cls.cluster.connect(
            ADMIN,
            cls.cluster.bootstrap_password,
        )

    def worker(self, role):
        return self.cluster.connect(
            role, self.passwords[role]
        )

    def review(
        self,
        conn,
        operation,
        tenant="tenant-one",
        connector="connector-one",
        account="provider-one",
    ):
        result = conn.execute(
            REVIEW,
            (operation, tenant, connector, account),
        ).fetchone()

        self.assertIsNotNone(result)
        self.assertEqual(len(result), 4)
        self.assertEqual(result[1:], (False, False, False))

        return result[0]

    def test_01_independent_worker_authentication(self):
        for role in WORKERS:
            with self.subTest(role=role):
                with self.worker(role) as conn:
                    identity = conn.execute(
                        """
                        SELECT session_user, current_user,
                               current_database()
                        """
                    ).fetchone()

                    self.assertEqual(
                        identity,
                        (role, role, DATABASE),
                    )

                    flags = conn.execute(
                        """
                        SELECT rolsuper, rolcreatedb,
                               rolcreaterole, rolinherit,
                               rolbypassrls, rolreplication
                        FROM pg_catalog.pg_roles
                        WHERE rolname = session_user
                        """
                    ).fetchone()

                    self.assertEqual(flags, (False,) * 6)

        with self.assertRaises(psycopg.OperationalError) as caught:
            with self.cluster.connect(
                WORKER_B,
                self.passwords[WORKER_A],
            ):
                pass

        error = caught.exception
        self.assertTrue(
            error.sqlstate == "28P01"
            or (
                "password authentication failed"
                in str(error).lower()
                and WORKER_B in str(error).lower()
            )
        )

        print("PASS: Worker credentials are independent")

    def test_02_exact_scope_and_cross_worker_denial(self):
        with self.worker(WORKER_A) as a:
            self.assertIs(
                self.review(a, self.operations[WORKER_A]),
                True,
            )
            self.assertIs(
                self.review(a, self.operations[WORKER_B]),
                False,
            )

        with self.worker(WORKER_B) as b:
            self.assertIs(
                self.review(b, self.operations[WORKER_B]),
                True,
            )
            self.assertIs(
                self.review(b, self.operations[WORKER_A]),
                False,
            )

        print("PASS: Correct-worker review only")
        print("PASS: Cross-worker review denied")
        print("PASS: Dispatch and retry remain denied")

    def test_03_forged_identity_and_wrong_scope_denied(self):
        with self.worker(WORKER_A) as worker:
            worker.execute(
                "SELECT set_config("
                "'safeact.worker_subject', %s, false)",
                (WORKER_B,),
            )

            self.assertEqual(
                worker.execute(
                    "SELECT current_setting("
                    "'safeact.worker_subject')"
                ).fetchone()[0],
                WORKER_B,
            )

            self.assertIs(
                self.review(worker, self.operations[WORKER_B]),
                False,
            )

            self.assertIs(
                self.review(
                    worker,
                    self.operations[WORKER_A],
                    tenant="wrong-tenant",
                ),
                False,
            )

            self.assertIs(
                self.review(
                    worker,
                    self.operations[WORKER_A],
                    connector="wrong-connector",
                ),
                False,
            )

            self.assertIs(
                self.review(
                    worker,
                    self.operations[WORKER_A],
                    account="wrong-account",
                ),
                False,
            )

            self.assertIs(
                self.review(worker, uuid4()),
                False,
            )

            self.assertIs(
                self.review(worker, self.operations[WORKER_A]),
                True,
            )

        print("PASS: Forged worker setting ignored")
        print("PASS: Tenant, connector, account and UUID scope checked")

    def test_04_direct_sql_and_role_escalation_denied(self):
        cases = [
            (
                WORKER_A,
                "Direct assignment read",
                "SELECT * FROM sa_worker.assignments",
                None,
            ),
            (
                WORKER_A,
                "Assignment insertion",
                "INSERT INTO sa_worker.assignments "
                "(operation_id,login_role,tenant_id,"
                "connector_id,provider_account_id,enabled) "
                "VALUES (%s,%s,'x','x','x',TRUE)",
                (uuid4(), WORKER_A),
            ),
            (
                WORKER_A,
                "Assignment update",
                "UPDATE sa_worker.assignments SET enabled=TRUE",
                None,
            ),
            (
                WORKER_A,
                "Assignment deletion",
                "DELETE FROM sa_worker.assignments",
                None,
            ),
            (
                WORKER_A,
                "Assignment truncate",
                "TRUNCATE sa_worker.assignments",
                None,
            ),
            (
                WORKER_A,
                "Disable table triggers",
                "ALTER TABLE sa_worker.assignments "
                "DISABLE TRIGGER ALL",
                None,
            ),
            (
                WORKER_A,
                "Grant assignment read",
                sql.SQL(
                    "GRANT SELECT ON sa_worker.assignments TO {}"
                ).format(sql.Identifier(WORKER_B)),
                None,
            ),
            (
                WORKER_A,
                "Create database role",
                "CREATE ROLE sa_worker_escalation LOGIN",
                None,
            ),
            (
                WORKER_A,
                "Create schema",
                "CREATE SCHEMA sa_worker_escalation",
                None,
            ),
            (
                WORKER_A,
                "Assume owner role",
                sql.SQL("SET ROLE {}").format(
                    sql.Identifier(OWNER)
                ),
                None,
            ),
            (
                WORKER_A,
                "Assume other worker role",
                sql.SQL("SET ROLE {}").format(
                    sql.Identifier(WORKER_B)
                ),
                None,
            ),
            (
                WORKER_A,
                "Change authenticated session identity",
                sql.SQL("SET SESSION AUTHORIZATION {}").format(
                    sql.Identifier(WORKER_B)
                ),
                None,
            ),
            (
                WORKER_A,
                "Change function owner",
                sql.SQL(
                    "ALTER FUNCTION "
                    "sa_worker.review_scope(uuid,text,text,text) "
                    "OWNER TO {}"
                ).format(sql.Identifier(WORKER_A)),
                None,
            ),
            (
                WORKER_B,
                "Other worker reads assignments",
                "SELECT * FROM sa_worker.assignments",
                None,
            ),
            (
                WORKER_B,
                "Other worker assumes worker A",
                sql.SQL("SET SESSION AUTHORIZATION {}").format(
                    sql.Identifier(WORKER_A)
                ),
                None,
            ),
            (
                ADMISSION,
                "Admission calls worker function",
                REVIEW,
                (
                    self.operations[WORKER_A],
                    "tenant-one",
                    "connector-one",
                    "provider-one",
                ),
            ),
            (
                STRANGER,
                "Unrelated principal calls worker function",
                REVIEW,
                (
                    self.operations[WORKER_B],
                    "tenant-one",
                    "connector-one",
                    "provider-one",
                ),
            ),
        ]

        self.assertEqual(len(cases), 17)
        denied = 0

        with self.admin() as admin:
            before = admin.execute(
                "SELECT operation_id,login_role,enabled "
                "FROM sa_worker.assignments "
                "ORDER BY operation_id"
            ).fetchall()

        for role, label, statement, params in cases:
            with self.subTest(label=label):
                if role in WORKERS:
                    connection = self.worker(role)
                else:
                    connection = self.cluster.connect(
                        role,
                        self.cluster.passwords[role],
                    )

                with connection as conn:
                    with self.assertRaises(
                        psycopg.Error,
                        msg="Forbidden SQL succeeded: " + label,
                    ) as caught:
                        conn.execute(statement, params)

                    self.assertEqual(
                        caught.exception.sqlstate,
                        "42501",
                        "Unexpected SQLSTATE: " + label,
                    )

                    denied += 1

        with self.admin() as admin:
            after = admin.execute(
                "SELECT operation_id,login_role,enabled "
                "FROM sa_worker.assignments "
                "ORDER BY operation_id"
            ).fetchall()

        self.assertEqual(before, after)
        self.assertEqual(denied, 17)

        print("PASS: 17/17 worker SQL denials")
        print("PASS: Unauthorized SQL left assignments unchanged")

    def test_05_committed_revocation_denies_future_review(self):
        operation = uuid4()

        with self.admin() as admin:
            admin.execute(
                """
                INSERT INTO sa_worker.assignments
                VALUES (%s,%s,%s,%s,%s,TRUE)
                """,
                (
                    operation,
                    WORKER_A,
                    "tenant-one",
                    "connector-one",
                    "provider-one",
                ),
            )

        self.additional_operations.add(operation)

        with self.worker(WORKER_A) as worker:
            self.assertIs(self.review(worker, operation), True)

        with self.admin() as admin:
            changed = admin.execute(
                """
                UPDATE sa_worker.assignments
                SET enabled=FALSE
                WHERE operation_id=%s
                  AND login_role=%s
                RETURNING enabled
                """,
                (operation, WORKER_A),
            ).fetchone()

            self.assertEqual(changed, (False,))

        with self.worker(WORKER_A) as worker:
            self.assertIs(self.review(worker, operation), False)

        print("PASS: Committed revocation denies new reviews")

    def test_06_row_lock_serializes_revocation(self):
        operation = uuid4()

        with self.admin() as admin:
            admin.execute(
                """
                INSERT INTO sa_worker.assignments
                VALUES (%s,%s,%s,%s,%s,TRUE)
                """,
                (
                    operation,
                    WORKER_A,
                    "tenant-one",
                    "connector-one",
                    "provider-one",
                ),
            )

        self.additional_operations.add(operation)

        worker = self.worker(WORKER_A)
        committed = False

        try:
            worker.execute("BEGIN")

            self.assertIs(
                self.review(worker, operation),
                True,
            )

            # This UPDATE must be blocked by the review's
            # FOR SHARE lock until the worker transaction ends.
            with self.admin() as admin:
                admin.execute(
                    "SET lock_timeout = '500ms'"
                )

                with self.assertRaises(
                    psycopg.errors.LockNotAvailable
                ):
                    admin.execute(
                        "UPDATE sa_worker.assignments "
                        "SET enabled=FALSE "
                        "WHERE operation_id=%s",
                        (operation,),
                    )

            worker.execute("COMMIT")
            committed = True

        finally:
            if not committed:
                try:
                    worker.execute("ROLLBACK")
                except psycopg.Error:
                    pass

            worker.close()

        with self.admin() as admin:
            changed = admin.execute(
                """
                UPDATE sa_worker.assignments
                SET enabled=FALSE
                WHERE operation_id=%s
                RETURNING enabled
                """,
                (operation,),
            ).fetchone()

            self.assertEqual(changed, (False,))

        with self.worker(WORKER_A) as worker:
            self.assertIs(
                self.review(worker, operation),
                False,
            )

        print("PASS: Review row lock blocked concurrent revocation")
        print("PASS: Revocation succeeded after lock release")
        print("PASS: New review denied after committed revocation")

    def test_07_independent_grants_and_state_observation(self):
        with self.admin() as admin:
            for role in WORKERS + (ADMISSION, STRANGER):
                allowed = admin.execute(
                    """
                    SELECT has_function_privilege(
                        %s,
                        'sa_worker.review_scope(uuid,text,text,text)',
                        'EXECUTE'
                    )
                    """,
                    (role,),
                ).fetchone()[0]

                self.assertEqual(
                    allowed,
                    role in WORKERS,
                )

            for role in WORKERS:
                access = admin.execute(
                    """
                    SELECT
                        has_table_privilege(
                            %s,
                            'sa_worker.assignments',
                            'SELECT'
                        ),
                        has_table_privilege(
                            %s,
                            'sa_worker.assignments',
                            'UPDATE'
                        )
                    """,
                    (role, role),
                ).fetchone()

                self.assertEqual(access, (False, False))

            memberships = admin.execute(
                """
                SELECT count(*)
                FROM pg_catalog.pg_auth_members m
                JOIN pg_catalog.pg_roles r
                  ON r.oid=m.member
                WHERE r.rolname IN (
                    'sa_iso_worker_a',
                    'sa_iso_worker_b'
                )
                """
            ).fetchone()[0]

            self.assertEqual(memberships, 0)

            rows = admin.execute(
                """
                SELECT operation_id,login_role,tenant_id,
                       connector_id,provider_account_id,enabled
                FROM sa_worker.assignments
                """
            ).fetchall()

            ids = {row[0] for row in rows}

            self.assertEqual(
                ids,
                set(self.operations.values())
                | self.additional_operations,
            )

            for row in rows:
                self.assertEqual(
                    row[2:5],
                    (
                        "tenant-one",
                        "connector-one",
                        "provider-one",
                    ),
                )

                if row[0] == self.operations[WORKER_A]:
                    self.assertEqual(row[1:], (
                        WORKER_A,
                        "tenant-one",
                        "connector-one",
                        "provider-one",
                        True,
                    ))

                if row[0] == self.operations[WORKER_B]:
                    self.assertEqual(row[1:], (
                        WORKER_B,
                        "tenant-one",
                        "connector-one",
                        "provider-one",
                        True,
                    ))

        print("PASS: Worker privilege matrix verified")
        print("PASS: No worker role memberships")
        print("PASS: Protected assignment inventory verified")
