"""Opt-in, isolated PostgreSQL credential security regressions.

Never uses the SafeAct development database.
Requires SAFEACT_RUN_ISOLATED_CLUSTER_TESTS=1.
All roles and protected objects are disposable synthetic fixtures.
This suite proves tested service boundaries, not per-worker authority.
"""

import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql


ADMIN = "sa_iso_bootstrap"
OWNER = "sa_iso_owner"
ADMISSION = "sa_iso_admission"
EXECUTION = "sa_iso_execution"
STRANGER = "sa_iso_stranger"
DATABASE = "sa_iso_fixture_db"

ROLES = (ADMISSION, EXECUTION, STRANGER)
TABLES = ("operations", "attempts", "audit_events", "outbox_events")


def temp_base():
    """The real (non-symlink) system temporary directory.

    macOS links /tmp to /private/tmp; Linux CI runners use /tmp itself.
    Every ownership, mode and free-space check applies either way.
    """
    if sys.platform == "darwin":
        return Path("/private/tmp").resolve(strict=True)
    return Path("/tmp").resolve(strict=True)


def run_process(args, timeout=90):
    result = subprocess.run(
        [str(item) for item in args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(
            "STOP: Disposable PostgreSQL process failed: "
            + str(args[0])
            + "\n"
            + result.stdout[-2000:]
        )
    return result.stdout


class DisposableCluster:
    """Owns one separately initialized PostgreSQL 14.23 cluster."""

    def __init__(self):
        self.root = None
        self.started = False
        self.start_attempted = False
        self.cleaned = False
        self.port = None
        self.passwords = {}
        self.operation_id = None
        self.nonce = secrets.token_hex(32)

    def connect(self, role, password, database=DATABASE):
        return psycopg.connect(
            host="127.0.0.1",
            port=self.port,
            dbname=database,
            user=role,
            password=password,
            connect_timeout=5,
            options="-c statement_timeout=5000",
            autocommit=True,
        )

    def assert_directory(self):
        root = self.root
        base = temp_base()

        if (
            root is None
            or not root.is_dir()
            or root.is_symlink()
            or root.parent != base
            or not root.name.startswith("safeact-regression-pg-")
            or root.stat().st_uid != os.geteuid()
            or stat.S_IMODE(root.stat().st_mode) != 0o700
        ):
            raise RuntimeError("STOP: Disposable directory identity mismatch")

        marker = root / "SAFEACT_DISPOSABLE_MARKER"
        if (
            not marker.is_file()
            or marker.is_symlink()
            or marker.read_text(encoding="utf-8")
            != self.nonce + "\n"
        ):
            raise RuntimeError("STOP: Disposable cleanup marker mismatch")

    def start(self):
        if os.geteuid() == 0:
            raise RuntimeError("STOP: PostgreSQL cannot run as root")

        self.binaries = {}

        for name in ("initdb", "pg_ctl", "postgres"):
            found = shutil.which(name)
            if not found:
                raise RuntimeError("STOP: Missing PostgreSQL binary " + name)

            binary = Path(found).resolve(strict=True)
            if "14.23" not in run_process([binary, "--version"], 10):
                raise RuntimeError("STOP: Unsupported PostgreSQL version")

            self.binaries[name] = binary

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]

        if self.port <= 1024 or self.port in (5432, 55432):
            raise RuntimeError("STOP: Invalid disposable port")

        base = temp_base()
        if (
            not base.is_dir()
            or not os.access(base, os.R_OK | os.W_OK | os.X_OK)
            or shutil.disk_usage(base).free < 512 * 1024 * 1024
        ):
            raise RuntimeError("STOP: Temporary filesystem unsuitable")

        old_umask = os.umask(0o077)
        try:
            self.root = Path(
                tempfile.mkdtemp(
                    prefix="safeact-regression-pg-",
                    dir=base,
                )
            ).resolve(strict=True)
        finally:
            os.umask(old_umask)

        root = self.root
        marker = root / "SAFEACT_DISPOSABLE_MARKER"
        marker.write_text(self.nonce + "\n", encoding="utf-8")
        marker.chmod(0o600)

        self.assert_directory()

        self.data = root / "data"
        self.socket_dir = root / "socket"
        self.log = root / "postgres.log"
        secret_file = root / "bootstrap.secret"

        self.socket_dir.mkdir(mode=0o700)

        self.bootstrap_password = secrets.token_urlsafe(40)
        self.passwords = {
            role: secrets.token_urlsafe(40)
            for role in ROLES
        }

        if len(set(self.passwords.values())) != 3:
            raise RuntimeError("STOP: Duplicate runtime credentials")

        fd = os.open(
            secret_file,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )

        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(self.bootstrap_password + "\n")

        try:
            run_process(
                [
                    self.binaries["initdb"],
                    "-D", self.data,
                    "-U", ADMIN,
                    "--encoding=UTF8",
                    "--locale=C",
                    "--auth-local=scram-sha-256",
                    "--auth-host=scram-sha-256",
                    "--pwfile", secret_file,
                ],
                timeout=90,
            )
        finally:
            secret_file.unlink(missing_ok=True)

        if (
            not self.data.is_dir()
            or self.data.is_symlink()
            or (self.data / "PG_VERSION").read_text().strip() != "14"
        ):
            raise RuntimeError("STOP: Invalid initialized cluster")

        if "'" in str(self.root) or " " in str(self.root):
            raise RuntimeError("STOP: Unsupported temporary path")

        with (self.data / "postgresql.conf").open(
            "a", encoding="utf-8"
        ) as config:
            config.write(
                "\n# SAFEACT DISPOSABLE REGRESSION CLUSTER\n"
                "listen_addresses = '127.0.0.1'\n"
                f"port = {self.port}\n"
                f"unix_socket_directories = '{self.socket_dir}'\n"
                "password_encryption = 'scram-sha-256'\n"
                "ssl = off\n"
                "max_connections = 20\n"
            )

        self.start_attempted = True

        run_process(
            [
                self.binaries["pg_ctl"],
                "-D", self.data,
                "-l", self.log,
                "-w", "-t", "45",
                "start",
            ],
            timeout=65,
        )

        self.started = True

        with self.connect(
            ADMIN, self.bootstrap_password, "postgres"
        ) as admin:
            identity = admin.execute(
                """
                SELECT current_database(), current_user,
                       session_user,
                       current_setting('data_directory'),
                       current_setting('listen_addresses'),
                       current_setting('port'),
                       current_setting('ssl')
                """
            ).fetchone()

            if (
                identity[0:3] != ("postgres", ADMIN, ADMIN)
                or Path(identity[3]).resolve() != self.data.resolve()
                or identity[4] != "127.0.0.1"
                or int(identity[5]) != self.port
                or identity[6] != "off"
            ):
                raise RuntimeError("STOP: Cluster identity mismatch")

            hba = admin.execute(
                """
                SELECT auth_method, error
                FROM pg_catalog.pg_hba_file_rules
                """
            ).fetchall()

            if not hba or any(
                method != "scram-sha-256" or error is not None
                for method, error in hba
            ):
                raise RuntimeError("STOP: Invalid SCRAM HBA rules")

            admin.execute(
                sql.SQL(
                    "CREATE ROLE {} NOLOGIN NOSUPERUSER "
                    "NOCREATEDB NOCREATEROLE NOINHERIT "
                    "NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(OWNER))
            )

            for role in ROLES:
                admin.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER "
                        "NOCREATEDB NOCREATEROLE NOINHERIT "
                        "NOREPLICATION NOBYPASSRLS PASSWORD {}"
                    ).format(
                        sql.Identifier(role),
                        sql.Literal(self.passwords[role]),
                    )
                )

            admin.execute(
                sql.SQL(
                    "CREATE DATABASE {} OWNER {}"
                ).format(
                    sql.Identifier(DATABASE),
                    sql.Identifier(OWNER),
                )
            )

            admin.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC")
                .format(sql.Identifier(DATABASE))
            )

            admin.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}")
                .format(
                    sql.Identifier(DATABASE),
                    sql.SQL(", ").join(
                        sql.Identifier(role) for role in ROLES
                    ),
                )
            )

        with self.connect(ADMIN, self.bootstrap_password) as admin:
            admin.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")

            admin.execute(
                sql.SQL("CREATE SCHEMA sa_fixture AUTHORIZATION {}")
                .format(sql.Identifier(OWNER))
            )

            for table in TABLES:
                admin.execute(
                    sql.SQL(
                        "CREATE TABLE sa_fixture.{} ("
                        "id uuid PRIMARY KEY, "
                        "operation_id uuid, label text NOT NULL)"
                    ).format(sql.Identifier(table))
                )
                admin.execute(
                    sql.SQL("ALTER TABLE sa_fixture.{} OWNER TO {}")
                    .format(
                        sql.Identifier(table),
                        sql.Identifier(OWNER),
                    )
                )

            admin.execute(
                sql.SQL(
                    "GRANT USAGE ON SCHEMA sa_fixture TO {}, {}"
                ).format(
                    sql.Identifier(ADMISSION),
                    sql.Identifier(EXECUTION),
                )
            )

            for table in (
                "operations", "audit_events", "outbox_events"
            ):
                admin.execute(
                    sql.SQL(
                        "GRANT SELECT, INSERT ON sa_fixture.{} TO {}"
                    ).format(
                        sql.Identifier(table),
                        sql.Identifier(ADMISSION),
                    )
                )

            admin.execute(
                """
                CREATE FUNCTION sa_fixture.deny_only_probe(
                    p_operation uuid
                )
                RETURNS boolean
                LANGUAGE sql
                STABLE
                SECURITY DEFINER
                SET search_path = pg_catalog, sa_fixture
                AS $function$
                    SELECT CASE
                        WHEN EXISTS (
                            SELECT 1 FROM sa_fixture.operations
                            WHERE operation_id = p_operation
                        )
                        THEN false
                        ELSE false
                    END
                $function$
                """
            )

            admin.execute(
                sql.SQL(
                    "ALTER FUNCTION sa_fixture.deny_only_probe(uuid) "
                    "OWNER TO {}"
                ).format(sql.Identifier(OWNER))
            )

            admin.execute(
                "REVOKE ALL ON FUNCTION "
                "sa_fixture.deny_only_probe(uuid) FROM PUBLIC"
            )

            admin.execute(
                sql.SQL(
                    "GRANT EXECUTE ON FUNCTION "
                    "sa_fixture.deny_only_probe(uuid) TO {}"
                ).format(sql.Identifier(EXECUTION))
            )

        self.operation_id = uuid4()

        with self.connect(
            ADMISSION, self.passwords[ADMISSION]
        ) as admission:
            for table in (
                "operations", "audit_events", "outbox_events"
            ):
                admission.execute(
                    sql.SQL(
                        "INSERT INTO sa_fixture.{} "
                        "(id, operation_id, label) "
                        "VALUES (%s, %s, %s)"
                    ).format(sql.Identifier(table)),
                    (uuid4(), self.operation_id, "test-only"),
                )

        print(
            "PASS: independent SCRAM cluster and "
            "synthetic fixture initialized",
            flush=True,
        )

    def cleanup(self):
        if self.root is None:
            return

        self.assert_directory()

        if self.start_attempted:
            status = subprocess.run(
                [
                    str(self.binaries["pg_ctl"]),
                    "-D", str(self.data), "status",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=15,
            )

            if status.returncode == 0:
                run_process(
                    [
                        self.binaries["pg_ctl"],
                        "-D", self.data,
                        "-m", "immediate",
                        "-w", "-t", "45",
                        "stop",
                    ],
                    timeout=65,
                )
            elif status.returncode != 3:
                print("PRESERVED CLUSTER:", self.root, flush=True)
                raise RuntimeError("STOP: Uncertain PostgreSQL status")

            final = subprocess.run(
                [
                    str(self.binaries["pg_ctl"]),
                    "-D", str(self.data), "status",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=15,
            )

            if final.returncode != 3:
                print("PRESERVED CLUSTER:", self.root, flush=True)
                raise RuntimeError(
                    "STOP: PostgreSQL not confirmed stopped"
                )

        self.assert_directory()
        shutil.rmtree(self.root)

        if self.root.exists():
            raise RuntimeError("STOP: Disposable directory not removed")

        self.cleaned = True
        print(
            "PASS: isolated cluster stopped and removed",
            flush=True,
        )


class PostgreSQLIdentityIsolationTests(unittest.TestCase):
    """Synthetic service-level identity isolation, never provider dispatch."""

    @classmethod
    def setUpClass(cls):
        if os.environ.get(
            "SAFEACT_RUN_ISOLATED_CLUSTER_TESTS"
        ) != "1":
            raise unittest.SkipTest(
                "Explicit isolated-cluster test opt-in required"
            )

        cls.cluster = DisposableCluster()
        cls.addClassCleanup(cls.cluster.cleanup)
        cls.cluster.start()

    def login(self, role):
        return self.cluster.connect(
            role, self.cluster.passwords[role]
        )

    def verify_role(self, conn, role):
        identity = conn.execute(
            """
            SELECT current_database(), current_user,
                   session_user, current_setting('port')
            """
        ).fetchone()

        self.assertEqual(
            identity,
            (DATABASE, role, role, str(self.cluster.port)),
        )

        flags = conn.execute(
            """
            SELECT rolsuper, rolcreatedb, rolcreaterole,
                   rolbypassrls, rolreplication, rolinherit
            FROM pg_catalog.pg_roles
            WHERE rolname = current_user
            """
        ).fetchone()

        self.assertEqual(flags, (False,) * 6)

    def test_01_independent_login_and_wrong_password(self):
        for role in ROLES:
            with self.subTest(role=role):
                with self.login(role) as conn:
                    self.verify_role(conn, role)

        with self.assertRaises(psycopg.OperationalError) as caught:
            with self.cluster.connect(
                EXECUTION, "incorrect-disposable-password"
            ):
                pass

        message = str(caught.exception).lower()
        self.assertTrue(
            caught.exception.sqlstate == "28P01"
            or (
                "password authentication failed" in message
                and EXECUTION in message
            ),
            "Wrong-password failure must be authentication-specific",
        )

        with self.login(EXECUTION) as conn:
            self.verify_role(conn, EXECUTION)

        print("PASS: independent credentials and password denial")

    def test_02_restricted_admission_positive_control(self):
        with self.login(ADMISSION) as admission:
            self.verify_role(admission, ADMISSION)

            for table in (
                "operations", "audit_events", "outbox_events"
            ):
                count = admission.execute(
                    sql.SQL("SELECT count(*) FROM sa_fixture.{}")
                    .format(sql.Identifier(table))
                ).fetchone()[0]

                self.assertEqual(count, 1)

            attempts_access = admission.execute(
                """
                SELECT has_table_privilege(
                    current_user,
                    'sa_fixture.attempts',
                    'INSERT'
                )
                """
            ).fetchone()[0]

            self.assertFalse(attempts_access)

        print("PASS: admission allowed access restricted")

    def test_03_deny_only_probe_and_untrusted_setting(self):
        with self.login(EXECUTION) as execution:
            self.verify_role(execution, EXECUTION)

            result = execution.execute(
                "SELECT sa_fixture.deny_only_probe(%s)",
                (self.cluster.operation_id,),
            ).fetchone()[0]

            self.assertIs(result, False)

            execution.execute(
                "SELECT set_config("
                "'safeact.worker_subject',"
                "'forged-worker', false)"
            )

            asserted = execution.execute(
                "SELECT current_setting('safeact.worker_subject')"
            ).fetchone()[0]

            self.assertEqual(asserted, "forged-worker")

            second = execution.execute(
                "SELECT sa_fixture.deny_only_probe(%s)",
                (self.cluster.operation_id,),
            ).fetchone()[0]

            self.assertIs(second, False)

        print("PASS: forged session setting grants no authority")

    def test_04_twenty_three_sql_privilege_denials(self):
        operation = self.cluster.operation_id

        cases = [
            (
                ADMISSION,
                "Admission read attempts",
                "SELECT * FROM sa_fixture.attempts",
                None,
            ),
            (
                ADMISSION,
                "Admission insert attempts",
                "INSERT INTO sa_fixture.attempts "
                "(id, operation_id, label) "
                "VALUES (%s, %s, 'forbidden')",
                (uuid4(), operation),
            ),
            (
                ADMISSION,
                "Admission update operations",
                "UPDATE sa_fixture.operations "
                "SET label = 'forbidden'",
                None,
            ),
            (
                ADMISSION,
                "Admission delete operations",
                "DELETE FROM sa_fixture.operations",
                None,
            ),
            (
                ADMISSION,
                "Admission truncate operations",
                "TRUNCATE sa_fixture.operations",
                None,
            ),
            (
                ADMISSION,
                "Admission execute worker function",
                "SELECT sa_fixture.deny_only_probe(%s)",
                (operation,),
            ),
            (
                ADMISSION,
                "Admission switch to owner",
                sql.SQL("SET ROLE {}").format(sql.Identifier(OWNER)),
                None,
            ),
            (
                ADMISSION,
                "Admission switch to execution",
                sql.SQL("SET ROLE {}").format(sql.Identifier(EXECUTION)),
                None,
            ),
            (
                EXECUTION,
                "Execution read operations",
                "SELECT * FROM sa_fixture.operations",
                None,
            ),
            (
                EXECUTION,
                "Execution insert operations",
                "INSERT INTO sa_fixture.operations "
                "(id, operation_id, label) "
                "VALUES (%s, %s, 'forbidden')",
                (uuid4(), operation),
            ),
            (
                EXECUTION,
                "Execution insert attempts",
                "INSERT INTO sa_fixture.attempts "
                "(id, operation_id, label) "
                "VALUES (%s, %s, 'forbidden')",
                (uuid4(), operation),
            ),
            (
                EXECUTION,
                "Execution update operations",
                "UPDATE sa_fixture.operations "
                "SET label = 'forbidden'",
                None,
            ),
            (
                EXECUTION,
                "Execution delete operations",
                "DELETE FROM sa_fixture.operations",
                None,
            ),
            (
                EXECUTION,
                "Execution truncate operations",
                "TRUNCATE sa_fixture.operations",
                None,
            ),
            (
                EXECUTION,
                "Execution disable triggers",
                "ALTER TABLE sa_fixture.operations "
                "DISABLE TRIGGER ALL",
                None,
            ),
            (
                EXECUTION,
                "Execution create role",
                "CREATE ROLE sa_iso_escalation LOGIN",
                None,
            ),
            (
                EXECUTION,
                "Execution create schema",
                "CREATE SCHEMA sa_iso_escalation",
                None,
            ),
            (
                EXECUTION,
                "Execution switch to owner",
                sql.SQL("SET ROLE {}").format(sql.Identifier(OWNER)),
                None,
            ),
            (
                EXECUTION,
                "Execution switch to admission",
                sql.SQL("SET ROLE {}").format(sql.Identifier(ADMISSION)),
                None,
            ),
            (
                STRANGER,
                "Stranger read operations",
                "SELECT * FROM sa_fixture.operations",
                None,
            ),
            (
                STRANGER,
                "Stranger execute function",
                "SELECT sa_fixture.deny_only_probe(%s)",
                (operation,),
            ),
            (
                STRANGER,
                "Stranger switch to execution",
                sql.SQL("SET ROLE {}").format(sql.Identifier(EXECUTION)),
                None,
            ),
            (
                STRANGER,
                "Stranger switch to owner",
                sql.SQL("SET ROLE {}").format(sql.Identifier(OWNER)),
                None,
            ),
        ]

        self.assertEqual(len(cases), 23)
        denied = 0

        for role, label, statement, params in cases:
            with self.subTest(role=role, statement=label):
                with self.login(role) as conn:
                    self.verify_role(conn, role)

                    try:
                        conn.execute(statement, params)
                    except psycopg.Error as exc:
                        self.assertEqual(
                            exc.sqlstate, "42501",
                            "Unexpected SQLSTATE for " + label,
                        )
                        denied += 1
                    else:
                        self.fail(
                            "Forbidden SQL succeeded: " + label
                        )

                print("PASS: DENIED:", label, flush=True)

        self.assertEqual(denied, 23)
        print("PASS: 23/23 isolated SQL denials")

    def test_05_function_permissions_and_role_inventory(self):
        with self.cluster.connect(
            ADMIN, self.cluster.bootstrap_password
        ) as admin:
            actual_roles = {
                row[0]
                for row in admin.execute(
                    "SELECT rolname FROM pg_catalog.pg_roles "
                    "WHERE rolname LIKE 'sa_iso_%'"
                ).fetchall()
            }

            self.assertEqual(
                actual_roles,
                {ADMIN, OWNER, ADMISSION, EXECUTION, STRANGER},
            )

            for role in ROLES:
                allowed = admin.execute(
                    """
                    SELECT has_function_privilege(
                        %s,
                        'sa_fixture.deny_only_probe(uuid)',
                        'EXECUTE'
                    )
                    """,
                    (role,),
                ).fetchone()[0]

                self.assertEqual(allowed, role == EXECUTION)

            memberships = admin.execute(
                """
                SELECT count(*)
                FROM pg_catalog.pg_auth_members m
                JOIN pg_catalog.pg_roles r
                  ON r.oid = m.member
                WHERE r.rolname IN (
                    'sa_iso_admission',
                    'sa_iso_execution',
                    'sa_iso_stranger'
                )
                """
            ).fetchone()[0]

            self.assertEqual(memberships, 0)

        print("PASS: exact roles, grants and memberships")

    def test_06_independent_protected_state_observation(self):
        expected_counts = {
            "operations": 1,
            "attempts": 0,
            "audit_events": 1,
            "outbox_events": 1,
        }

        with self.cluster.connect(
            ADMIN, self.cluster.bootstrap_password
        ) as admin:
            for table, expected in expected_counts.items():
                count = admin.execute(
                    sql.SQL("SELECT count(*) FROM sa_fixture.{}")
                    .format(sql.Identifier(table))
                ).fetchone()[0]

                self.assertEqual(
                    count, expected,
                    "Protected fixture modified: " + table,
                )

            row = admin.execute(
                "SELECT operation_id, label "
                "FROM sa_fixture.operations"
            ).fetchone()

            self.assertEqual(
                row, (self.cluster.operation_id, "test-only")
            )

        print("PASS: independent protected state preserved")
