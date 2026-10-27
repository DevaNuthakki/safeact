"""Passwords and session secrets never reach the PostgreSQL log.

Opt-in (SAFEACT_RUN_ISOLATED_CLUSTER_TESTS=1). Starts a throwaway
PostgreSQL 14.23 cluster, switches on the noisiest logging the server
has (every statement, every parameter, every duration, statements of
failed calls), and proves it is on with a marker that must appear.
Then it builds the 0011 identity objects, gives the API role the
no-secret logging settings, and drives sign-in (right, wrong and
unknown), whoami, a refused and a successful logout and a revoked
session as that role. The server log must contain none of the
passwords, the session secret or the CSRF token.
"""

import os
import secrets
import unittest
import uuid

from psycopg import sql

from storage import product_api as product
from storage.identity_admin import create_principal, role_logging_statements
from storage.lifecycle_sql import v0011
from tests.isolated.test_postgresql_identity_isolation import (
    ADMIN,
    DATABASE,
    DisposableCluster,
)


API = "safeact_iso_api"
API_OWNER = "safeact_iso_api_owner"
# Control: same grants, default logging. Its secrets must leak.
LOUD_API = "safeact_iso_api_loud"
SCHEMA = "safeact_it_" + uuid.uuid4().hex

LOUD_LOGGING = {
    "log_statement": "all",
    "log_duration": "on",
    "log_min_duration_statement": "0",
    "log_parameter_max_length": "-1",
    "log_parameter_max_length_on_error": "-1",
    "log_min_error_statement": "error",
    # Name the user on every line so each role's lines can be told apart.
    "log_line_prefix": "%m [%p] user=%u ",
}

REJECT_MUTATION = """
CREATE FUNCTION public.safeact_reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $safeact$
BEGIN
    RAISE EXCEPTION 'SafeAct foundation rows are insert-only: %',
        TG_TABLE_NAME USING ERRCODE = 'check_violation';
    RETURN NULL;
END;
$safeact$
"""


class ApiSecretLoggingTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if os.environ.get("SAFEACT_RUN_ISOLATED_CLUSTER_TESTS") != "1":
            raise unittest.SkipTest("Explicit disposable-cluster opt-in required")

        cls.cluster = DisposableCluster()
        cls.addClassCleanup(cls.cluster.cleanup)
        cls.cluster.start()
        cls.api_password = secrets.token_urlsafe(32)
        cls.user_password = "pw-" + secrets.token_hex(12)
        cls.wrong_password = "wrong-" + secrets.token_hex(12)
        cls.marker = "safeact-log-marker-" + secrets.token_hex(8)

        with cls.cluster.connect(ADMIN, cls.cluster.bootstrap_password) as admin:
            for name, value in LOUD_LOGGING.items():
                admin.execute(sql.SQL("ALTER SYSTEM SET {} = {}").format(
                    sql.Identifier(name), sql.Literal(value)
                ))
            admin.execute("SELECT pg_reload_conf()")

            admin.execute("CREATE EXTENSION pgcrypto SCHEMA public")
            admin.execute(REJECT_MUTATION)
            admin.execute(sql.SQL(
                "CREATE ROLE {} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            ).format(sql.Identifier(API_OWNER)))
            for role in (API, LOUD_API):
                admin.execute(sql.SQL(
                    "CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(role), sql.Literal(cls.api_password)))
                admin.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(DATABASE), sql.Identifier(role)
                ))
            for statement in role_logging_statements(API):
                admin.execute(statement)
            admin.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                sql.Identifier(API_OWNER)
            ))

            s = sql.Identifier(SCHEMA)
            with admin.transaction():
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(s))
                for statement in v0011.upgrade_statements(SCHEMA):
                    admin.execute(statement)
                admin.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}, {}, {}").format(
                    s, sql.Identifier(API_OWNER), sql.Identifier(API),
                    sql.Identifier(LOUD_API),
                ))
                for table, privileges in v0011.OWNER_TABLE_PRIVILEGES.items():
                    admin.execute(sql.SQL("GRANT {} ON {}.{} TO {}").format(
                        sql.SQL(", ").join(sql.SQL(p) for p in privileges),
                        s, sql.Identifier(table), sql.Identifier(API_OWNER),
                    ))
                for name, args in v0011.NEW_FUNCTIONS.items():
                    admin.execute(sql.SQL("ALTER FUNCTION {}.{}({}) OWNER TO {}").format(
                        s, sql.Identifier(name), sql.SQL(args),
                        sql.Identifier(API_OWNER),
                    ))
                for name, args in v0011.API_FUNCTIONS.items():
                    admin.execute(sql.SQL(
                        "GRANT EXECUTE ON FUNCTION {}.{}({}) TO {}, {}"
                    ).format(
                        s, sql.Identifier(name), sql.SQL(args),
                        sql.Identifier(API), sql.Identifier(LOUD_API),
                    ))
                create_principal(
                    admin, SCHEMA, tenant_id="tenant-iso",
                    username="iso-reviewer", display_name="Isolated Reviewer",
                    roles=("reviewer",), password=cls.user_password,
                )

            # Proves the loud settings are live: this parameter must be
            # logged.
            admin.execute("SELECT %s::text", (cls.marker,))

        cls.secret, cls.csrf = cls.drive_api()
        cls.leaked_password = "leaks-" + secrets.token_hex(12)
        with cls.cluster.connect(LOUD_API, cls.api_password) as loud:
            product.login(
                loud, SCHEMA, username="iso-nobody",
                password=cls.leaked_password, source="127.0.0.1",
            )
        cls.log = cls.cluster.log.read_text(encoding="utf-8", errors="replace")

    @classmethod
    def drive_api(cls):
        def call(action):
            with cls.cluster.connect(API, cls.api_password) as connection:
                connection.autocommit = False
                try:
                    result = action(connection)
                    connection.commit()
                    return result
                except product.ProductRefused:
                    connection.rollback()
                    return None

        def login(username, password):
            return call(lambda c: product.login(
                c, SCHEMA, username=username, password=password,
                source="127.0.0.1",
            ))

        assert not login("iso-reviewer", cls.wrong_password).ok
        assert not login("iso-nobody", cls.wrong_password).ok
        signed_in = login("iso-reviewer", cls.user_password)
        assert signed_in.ok
        secret, csrf = signed_in.session_secret, signed_in.csrf_token

        assert call(lambda c: product.whoami(c, SCHEMA, secret=secret))
        # Refused calls: their statements would carry the secret.
        assert call(lambda c: product.logout(
            c, SCHEMA, secret=secret, csrf="0" * 64
        )) is None
        assert call(lambda c: product.logout(
            c, SCHEMA, secret=secret, csrf=csrf
        ))
        assert call(lambda c: product.whoami(c, SCHEMA, secret=secret)) is None
        return secret, csrf

    def test_logging_was_actually_on(self):
        self.assertIn(self.marker, self.log)
        # Control: without the role settings a password is logged.
        self.assertIn(self.leaked_password, self.log)
        # The API role's failures are still visible as errors.
        self.assertIn("CSRF token refused", self.log)
        self.assertIn("Not authenticated", self.log)

    def test_no_secret_reaches_the_log(self):
        for name, value in {
            "user password": self.user_password,
            "wrong password": self.wrong_password,
            "session secret": self.secret,
            "csrf token": self.csrf,
        }.items():
            with self.subTest(secret=name):
                self.assertNotIn(value, self.log)

    def lines_of(self, role):
        return [
            line for line in self.log.splitlines()
            if f" user={role} " in line
        ]

    def test_api_role_statements_are_not_logged_at_all(self):
        api = self.lines_of(API)
        # Its refusals are still logged as errors...
        self.assertTrue(any("ERROR:" in line for line in api))
        # ...but never a statement, its parameters or a duration.
        for line in api:
            for leak in ("statement:", "execute ", "parameters:", "duration:"):
                self.assertNotIn(leak, line)

    def test_control_role_without_settings_logs_its_calls(self):
        loud = self.lines_of(LOUD_API)
        self.assertTrue(any("parameters:" in line for line in loud))
        self.assertTrue(any(
            f'"{SCHEMA}"."safeact_api_login"' in line for line in loud
        ))


if __name__ == "__main__":
    unittest.main()
