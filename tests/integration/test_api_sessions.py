"""Sessions verified in PostgreSQL (Milestone 3, D2).

Sign-in, throttling, expiry, revocation, CSRF, cookies, hash-only
storage, the API role's privileges and logging settings, and the
identity rules (service principals, demo principals, reviewers).
Each test gets a fresh disposable environment so throttle counters
never leak between tests. Requires SAFEACT_RUN_DB_INTEGRATION=1 and
SAFEACT_RUN_DISPOSABLE_DB_TESTS=1.
"""

import hashlib
import os
import secrets
import statistics
import threading
import time
import unittest

from http.cookies import SimpleCookie

import psycopg
from psycopg import sql

from api.auth import SIGN_IN_FAILED
from api.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from storage import durable_lifecycle_sql as lsql
from storage import product_api as product
from storage.database import local_admin_settings
from storage.identity_admin import (
    APPLICATION_IDLE_TIMEOUT, NO_SECRET_LOGGING, create_principal,
)
from storage.lifecycle_sql import v0011
from tests.integration.api_fixture import ApiEnvironment


ENABLED = (
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1"
)

REQUESTER = "demo-north-requester"
REVIEWER = "demo-north-reviewer"
SOUTH = "demo-south-requester"
AGENT = "demo-north-agent"


@unittest.skipUnless(ENABLED, "PostgreSQL integration tests are opt-in")
class SessionTestCase(unittest.TestCase):

    def setUp(self):
        self.api = ApiEnvironment().start()
        self.addCleanup(self.api.cleanup)

    # -- helpers -----------------------------------------------------

    def login(self, client, username, password):
        return client.post(
            "/auth/login", json={"username": username, "password": password}
        )

    def outcomes(self, username=None):
        where, params = sql.SQL("true"), ()
        if username is not None:
            where = sql.SQL("principal_id = %s")
            params = (self.api.principal_id(username),)
        return [row[0] for row in self.api.rows(
            sql.SQL(
                "SELECT outcome FROM {} WHERE {} ORDER BY attempt_id"
            ).format(self.api.table("safeact_login_attempts"), where),
            params,
        )]

    def backdate_failures(self, username, count, minutes_ago):
        principal = self.api.principal_id(username)
        with self.api.admin.transaction():
            for _ in range(count):
                self.api.admin.execute(sql.SQL(
                    "INSERT INTO {} (username_key, principal_id, source, "
                    "outcome, created_at) VALUES (encode(sha256("
                    "convert_to(%s, 'UTF8')), 'hex'), %s, 'backdated', "
                    "'bad_credentials', now() - make_interval(mins => %s))"
                ).format(self.api.table("safeact_login_attempts")),
                    (username, principal, minutes_ago))

    def forge_session(self, username, *, created_ago, expires_in,
                      seen_ago=None):
        """Insert a session as the administrator; return its secret."""

        secret, csrf = secrets.token_hex(32), secrets.token_hex(32)
        session = self.api.admin.execute(sql.SQL(
            "INSERT INTO {} (session_id, principal_id, secret_sha256, "
            "csrf_sha256, created_at, expires_at) VALUES (gen_random_uuid(),"
            " %s, sha256(convert_to(%s, 'UTF8')), sha256(convert_to(%s, "
            "'UTF8')), now() - %s::interval, now() + %s::interval) "
            "RETURNING session_id"
        ).format(self.api.table("safeact_sessions")), (
            self.api.principal_id(username), secret, csrf,
            created_ago, expires_in,
        )).fetchone()[0]
        if seen_ago is not None:
            self.api.admin.execute(sql.SQL(
                "INSERT INTO {} (session_id, seen_at) "
                "VALUES (%s, now() - %s::interval)"
            ).format(self.api.table("safeact_session_activity")),
                (session, seen_ago))
        return secret, csrf

    def whoami(self, secret):
        with self.api.connect_api() as connection:
            with connection.transaction():
                return product.whoami(
                    connection, self.api.schema, secret=secret
                )


class SignInTests(SessionTestCase):

    def test_sign_in_returns_the_principal_derived_in_sql(self):
        client = self.api.client()
        response = self.login(client, REVIEWER, self.api.passwords[REVIEWER])
        self.assertEqual(response.status_code, 200)
        principal = response.json()["principal"]
        self.assertEqual(principal["tenant_id"], "tenant-north")
        self.assertEqual(principal["roles"], ["reviewer"])
        self.assertEqual(principal["kind"], "human")
        self.assertTrue(principal["demo"])
        self.assertRegex(response.json()["csrf_token"], r"^[0-9a-f]{64}$")

        me = client.get("/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["username"], REVIEWER)
        self.assertEqual(self.outcomes(REVIEWER), ["success"])

    def test_failures_are_byte_identical_whatever_the_cause(self):
        client = self.api.client()
        bodies = {
            "wrong password": self.login(client, REQUESTER, "not-it"),
            "unknown user": self.login(client, "demo-nobody", "not-it"),
            "service principal": self.login(client, AGENT, "not-it"),
            "too long username": self.login(client, "x" * 200, "not-it"),
            "mixed case": self.login(
                client, REQUESTER.upper(), "definitely-wrong"
            ),
        }
        for _ in range(5):
            self.login(client, REVIEWER, "not-it")
        bodies["locked account"] = self.login(
            client, REVIEWER, self.api.passwords[REVIEWER]
        )

        expected = self.api.client().post(
            "/auth/login", json={"username": "demo-nobody", "password": "x"}
        ).content
        for cause, response in bodies.items():
            with self.subTest(cause=cause):
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.content, expected)
                self.assertEqual(response.json(), SIGN_IN_FAILED)
                self.assertNotIn("set-cookie", response.headers)

    def test_username_is_case_insensitive(self):
        response = self.login(
            self.api.client(), REQUESTER.upper(),
            self.api.passwords[REQUESTER],
        )
        self.assertEqual(response.status_code, 200)

    def test_service_principal_cannot_sign_in_or_hold_a_password(self):
        response = self.login(self.api.client(), AGENT, "anything")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.outcomes()[-1], "unknown_user")

        agent = self.api.principal_id(AGENT)
        with self.assertRaises(psycopg.Error) as caught:
            self.api.admin.execute(sql.SQL(
                "INSERT INTO {} (principal_id, password_hash) VALUES "
                "(%s, public.crypt('x', public.gen_salt('bf', 12)))"
            ).format(self.api.table("safeact_credentials")), (agent,))
        self.assertEqual(caught.exception.sqlstate, lsql.IDENTITY_INVALID)

        with self.assertRaises(psycopg.Error) as caught:
            self.api.admin.execute(sql.SQL(
                "INSERT INTO {} (principal_id, role) VALUES (%s, 'reviewer')"
            ).format(self.api.table("safeact_role_grants")), (agent,))
        self.assertEqual(caught.exception.sqlstate, lsql.IDENTITY_INVALID)

    def test_disabled_principal_loses_sessions_and_sign_in(self):
        client, _ = self.api.signed_in(REQUESTER)
        self.api.admin.execute(sql.SQL(
            "INSERT INTO {} (principal_id) VALUES (%s)"
        ).format(self.api.table("safeact_principal_disablements")),
            (self.api.principal_id(REQUESTER),))
        self.assertEqual(client.get("/auth/me").status_code, 401)
        self.assertEqual(
            self.login(
                self.api.client(), REQUESTER, self.api.passwords[REQUESTER]
            ).status_code, 401,
        )


class ThrottleTests(SessionTestCase):

    def test_five_committed_failures_lock_even_the_right_password(self):
        client = self.api.client()
        for _ in range(5):
            self.assertEqual(
                self.login(client, REQUESTER, "wrong").status_code, 401
            )
        # Every failure committed: the API's transaction kept the row.
        self.assertEqual(self.outcomes(REQUESTER), ["bad_credentials"] * 5)

        response = self.login(client, REQUESTER, self.api.passwords[REQUESTER])
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.outcomes(REQUESTER)[-1], "locked")
        self.assertEqual(self.api.count("safeact_sessions"), 0)

    def test_four_failures_do_not_lock(self):
        client = self.api.client()
        for _ in range(4):
            self.login(client, REQUESTER, "wrong")
        response = self.login(client, REQUESTER, self.api.passwords[REQUESTER])
        self.assertEqual(response.status_code, 200)

    def test_success_starts_a_new_count(self):
        client = self.api.client()
        for _ in range(4):
            self.login(client, REQUESTER, "wrong")
        good = self.api.passwords[REQUESTER]
        self.assertEqual(self.login(client, REQUESTER, good).status_code, 200)
        for _ in range(4):
            self.login(client, REQUESTER, "wrong")
        self.assertEqual(self.login(client, REQUESTER, good).status_code, 200)

    def test_lock_lifts_once_failures_leave_the_window(self):
        self.backdate_failures(REQUESTER, 5, minutes_ago=16)
        self.backdate_failures(REVIEWER, 5, minutes_ago=14)
        client = self.api.client()
        self.assertEqual(self.login(
            client, REQUESTER, self.api.passwords[REQUESTER]
        ).status_code, 200)
        self.assertEqual(self.login(
            client, REVIEWER, self.api.passwords[REVIEWER]
        ).status_code, 401)
        self.assertEqual(self.outcomes(REVIEWER)[-1], "locked")

    def test_concurrent_guesses_are_all_counted(self):
        def guess():
            with self.api.connect_api() as connection:
                with connection.transaction():
                    product.login(
                        connection, self.api.schema, username=REQUESTER,
                        password="wrong", source="parallel",
                    )

        threads = [threading.Thread(target=guess) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        outcomes = self.outcomes(REQUESTER)
        self.assertEqual(len(outcomes), 8)
        # The advisory lock serializes them: no more than five are ever
        # checked against the password; the rest are refused as locked.
        self.assertEqual(outcomes.count("bad_credentials"), 5)
        self.assertEqual(outcomes.count("locked"), 3)

    def test_one_source_is_throttled_across_usernames(self):
        with self.api.connect_api() as connection:
            for index in range(v0011.SOURCE_FAILURES):
                with connection.transaction():
                    product.login(
                        connection, self.api.schema,
                        username=f"demo-unknown-{index}", password="x",
                        source="203.0.113.7",
                    )
            with connection.transaction():
                refused = product.login(
                    connection, self.api.schema, username=SOUTH,
                    password=self.api.passwords[SOUTH], source="203.0.113.7",
                )
            with connection.transaction():
                elsewhere = product.login(
                    connection, self.api.schema, username=SOUTH,
                    password=self.api.passwords[SOUTH], source="198.51.100.2",
                )
        self.assertFalse(refused.ok)
        self.assertTrue(elsewhere.ok)

    def test_every_path_runs_exactly_one_bcrypt_check(self):
        for _ in range(5):
            self.login(self.api.client(), REVIEWER, "wrong")
        cases = {
            "unknown user": ("demo-nobody", "x"),
            "service principal": (AGENT, "x"),
            "wrong password": (REQUESTER, "wrong"),
            "right password": (REQUESTER, self.api.passwords[REQUESTER]),
            "locked": (REVIEWER, self.api.passwords[REVIEWER]),
            "invalid": ("y" * 100, "x"),
        }
        matcher = sql.SQL("{}.safeact_password_matches(text, text)").format(
            sql.Identifier(self.api.schema)
        ).as_string(self.api.admin)
        for case, (username, password) in cases.items():
            with self.subTest(case=case):
                with psycopg.connect(**local_admin_settings()) as admin:
                    admin.execute("SET track_functions = 'pl'")
                    admin.execute(sql.SQL(
                        "SELECT * FROM {}.safeact_api_login(%s, %s, 'probe')"
                    ).format(sql.Identifier(self.api.schema)),
                        (username, password))
                    calls = admin.execute(
                        "SELECT pg_stat_get_xact_function_calls(%s::regprocedure)",
                        (matcher,),
                    ).fetchone()[0]
                    admin.rollback()
                self.assertEqual(calls, 1)

    def test_unknown_user_costs_about_as_much_as_a_wrong_password(self):
        def timed(username):
            with self.api.connect_api() as connection:
                started = time.perf_counter()
                with connection.transaction():
                    product.login(
                        connection, self.api.schema, username=username,
                        password="wrong", source=secrets.token_hex(4),
                    )
                return time.perf_counter() - started

        unknown = statistics.median(timed("demo-nobody") for _ in range(3))
        wrong = statistics.median(timed(SOUTH) for _ in range(3))
        # Coarse: both pay one bcrypt-12 check (~0.1-0.3 s); a skipped
        # check would make the unknown case an order of magnitude faster.
        self.assertGreater(unknown / wrong, 0.5)
        self.assertLess(unknown / wrong, 2.0)


class SessionLifetimeTests(SessionTestCase):

    def test_absolute_expiry(self):
        secret, _ = self.forge_session(
            REQUESTER, created_ago="9 hours", expires_in="-1 hour",
            seen_ago="1 minute",
        )
        with self.assertRaises(product.Unauthenticated):
            self.whoami(secret)

    def test_idle_expiry_and_activity(self):
        idle, _ = self.forge_session(
            REQUESTER, created_ago="1 hour", expires_in="7 hours"
        )
        with self.assertRaises(product.Unauthenticated):
            self.whoami(idle)

        active, _ = self.forge_session(
            REQUESTER, created_ago="1 hour", expires_in="7 hours",
            seen_ago="10 minutes",
        )
        self.assertEqual(self.whoami(active).username, REQUESTER)

        stale, _ = self.forge_session(
            REQUESTER, created_ago="2 hours", expires_in="6 hours",
            seen_ago="31 minutes",
        )
        with self.assertRaises(product.Unauthenticated):
            self.whoami(stale)

    def test_activity_is_recorded_at_most_once_a_minute(self):
        client, _ = self.api.signed_in(REQUESTER)
        for _ in range(5):
            self.assertEqual(client.get("/auth/me").status_code, 200)
        self.assertEqual(self.api.count("safeact_session_activity"), 0)

        secret, _ = self.forge_session(
            REQUESTER, created_ago="5 minutes", expires_in="7 hours"
        )
        for _ in range(3):
            self.whoami(secret)
        self.assertEqual(self.api.count("safeact_session_activity"), 1)

    def test_logout_revokes_and_clears_cookies(self):
        client, csrf = self.api.signed_in(REQUESTER)
        secret = client.cookies.get(SESSION_COOKIE)
        response = client.post("/auth/logout", headers={CSRF_HEADER: csrf})
        self.assertEqual(response.status_code, 204)
        cleared = SimpleCookie()
        for header in response.headers.get_list("set-cookie"):
            cleared.load(header)
        self.assertEqual(cleared[SESSION_COOKIE]["max-age"], "0")
        self.assertEqual(cleared[CSRF_COOKIE]["max-age"], "0")

        with self.assertRaises(product.Unauthenticated):
            self.whoami(secret)
        replay = self.api.client()
        replay.cookies.set(SESSION_COOKIE, secret)
        self.assertEqual(replay.get("/auth/me").status_code, 401)

    def test_malformed_and_missing_secrets_are_unauthenticated(self):
        for secret in (None, "", "zz", "0" * 64, "A" * 64, "0" * 63):
            with self.subTest(secret=secret):
                with self.assertRaises(product.Unauthenticated):
                    self.whoami(secret)
        self.assertEqual(self.api.client().get("/auth/me").status_code, 401)


class CsrfAndCookieTests(SessionTestCase):

    def test_cookie_flags(self):
        response = self.login(
            self.api.client(), REQUESTER, self.api.passwords[REQUESTER]
        )
        headers = {
            h.split("=", 1)[0]: h for h in response.headers.get_list("set-cookie")
        }
        session = headers[SESSION_COOKIE].lower()
        csrf = headers[CSRF_COOKIE].lower()
        for header in (session, csrf):
            self.assertIn("secure", header)
            self.assertIn("samesite=strict", header)
            self.assertIn("path=/", header)
            self.assertNotIn("domain=", header)
        self.assertIn("httponly", session)
        self.assertNotIn("httponly", csrf)
        self.assertTrue(SESSION_COOKIE.startswith("__Host-"))
        self.assertTrue(CSRF_COOKIE.startswith("__Host-"))

    def test_csrf_missing_invalid_or_from_another_session(self):
        client, csrf = self.api.signed_in(REQUESTER)
        _, other_csrf = self.api.signed_in(REVIEWER)
        for header in (None, "", "0" * 64, secrets.token_hex(32), other_csrf):
            with self.subTest(header=header):
                headers = {} if header is None else {CSRF_HEADER: header}
                response = client.post("/auth/logout", headers=headers)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error"]["code"], "csrf_refused")
        self.assertEqual(client.get("/auth/me").status_code, 200)
        self.assertEqual(
            client.post("/auth/logout", headers={CSRF_HEADER: csrf}).status_code,
            204,
        )

    def test_cross_site_writes_are_refused_before_sign_in(self):
        client = self.api.client()
        good = {"username": REQUESTER, "password": self.api.passwords[REQUESTER]}
        for headers in (
            {"Origin": "https://evil.example"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
        ):
            with self.subTest(headers=headers):
                response = client.post("/auth/login", json=good, headers=headers)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(
                    response.json()["error"]["code"], "cross_site_refused"
                )
        self.assertEqual(self.outcomes(), [])

        for headers in (
            {"Origin": "http://localhost:5173", "Sec-Fetch-Site": "same-origin"},
            {"Origin": "https://testserver"},
            {},
        ):
            with self.subTest(headers=headers):
                response = client.post("/auth/login", json=good, headers=headers)
                self.assertEqual(response.status_code, 200)


class StorageAndPrivilegeTests(SessionTestCase):

    IDENTITY_TABLES = v0011.NEW_TABLES

    def test_database_holds_only_hashes(self):
        client, csrf = self.api.signed_in(REQUESTER)
        secret = client.cookies.get(SESSION_COOKIE)
        password = self.api.passwords[REQUESTER]
        dump = "\n".join(
            row[0] for table in self.IDENTITY_TABLES
            for row in self.api.rows(sql.SQL(
                "SELECT row_to_json(t)::text FROM {} AS t"
            ).format(self.api.table(table)))
        )
        self.assertIn(REQUESTER, dump)
        for value in (secret, csrf, password):
            self.assertNotIn(value, dump)
        # SHA-256 digests of the secrets are what is stored, as bytea.
        stored = self.api.rows(sql.SQL(
            "SELECT encode(secret_sha256, 'hex'), encode(csrf_sha256, 'hex') "
            "FROM {}"
        ).format(self.api.table("safeact_sessions")))
        self.assertEqual(stored, [(
            hashlib.sha256(secret.encode()).hexdigest(),
            hashlib.sha256(csrf.encode()).hexdigest(),
        )])

    def test_every_password_hash_is_bcrypt_cost_12(self):
        hashes = [row[0] for row in self.api.rows(sql.SQL(
            "SELECT password_hash FROM {}"
        ).format(self.api.table("safeact_credentials")))]
        self.assertEqual(len(hashes), 7)
        for value in hashes:
            self.assertTrue(value.startswith("$2a$12$"))

        with self.api.admin.transaction():
            principal = create_principal(
                self.api.admin, self.api.schema, tenant_id="tenant-north",
                username="demo-north-weak", display_name="Weak",
            )
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.api.admin.execute(sql.SQL(
                "INSERT INTO {} (principal_id, password_hash) VALUES "
                "(%s, public.crypt('x', public.gen_salt('bf', 10)))"
            ).format(self.api.table("safeact_credentials")), (principal,))

    def test_identity_tables_are_append_only(self):
        self.api.signed_in(REQUESTER)
        for table in ("safeact_sessions", "safeact_principals",
                      "safeact_login_attempts", "safeact_credentials"):
            with self.subTest(table=table):
                with self.assertRaises(psycopg.Error):
                    self.api.admin.execute(sql.SQL(
                        "DELETE FROM {}"
                    ).format(self.api.table(table)))

    def test_api_role_cannot_read_tables_or_call_internal_functions(self):
        with self.api.connect_api() as api:
            for table in self.IDENTITY_TABLES + (
                "safeact_operations", "safeact_audit_events",
            ):
                with self.subTest(table=table):
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        api.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(
                            self.api.table(table)
                        ))
            for call in (
                "safeact_api_session(NULL, NULL, false, NULL)",
                "safeact_password_matches('a', 'b')",
                "safeact_accounting()",
                "safeact_claimable_operations(1)",
            ):
                with self.subTest(call=call):
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        api.execute(sql.SQL("SELECT * FROM {}." + call).format(
                            sql.Identifier(self.api.schema)
                        ))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                api.execute("SET log_statement = 'all'")

    def test_api_role_logs_no_statements_or_parameters(self):
        settings = self.api.rows(
            "SELECT unnest(setconfig) FROM pg_db_role_setting "
            "WHERE setrole = %s::regrole AND setdatabase = 0",
            (self.api.env.api,),
        )
        self.assertEqual(
            dict(row[0].split("=", 1) for row in settings),
            NO_SECRET_LOGGING | {
                "idle_in_transaction_session_timeout": APPLICATION_IDLE_TIMEOUT,
            },
        )
        with self.api.connect_api() as api:
            for name, value in NO_SECRET_LOGGING.items():
                with self.subTest(setting=name):
                    shown = api.execute(sql.SQL("SHOW {}").format(
                        sql.Identifier(name)
                    )).fetchone()[0]
                    self.assertEqual(shown, value)

    def test_another_tenant_cannot_be_chosen_without_its_secret(self):
        north_client, _ = self.api.signed_in(REQUESTER)
        south_client, _ = self.api.signed_in(SOUTH)
        self.assertEqual(
            north_client.get("/auth/me").json()["tenant_id"], "tenant-north"
        )
        self.assertEqual(
            south_client.get("/auth/me").json()["tenant_id"], "tenant-south"
        )
        # Direct SQL as the API role: the only input is a secret, and a
        # guessed one identifies nobody.
        for _ in range(5):
            with self.assertRaises(product.Unauthenticated):
                self.whoami(secrets.token_hex(32))

    def test_demo_principals_are_refused_outside_demo_schemas(self):
        with self.assertRaises(psycopg.Error) as caught:
            with self.api.admin.transaction():
                create_principal(
                    self.api.admin, "public", tenant_id="tenant-north",
                    username="demo-north-sneaky", display_name="Sneaky",
                )
        self.assertEqual(caught.exception.sqlstate, lsql.IDENTITY_INVALID)

        with self.assertRaises(psycopg.errors.CheckViolation):
            self.api.admin.execute(sql.SQL(
                "INSERT INTO {} (principal_id, tenant_id, username, kind, "
                "display_name, demo) VALUES (gen_random_uuid(), 'tenant-north', "
                "'demo-north-mislabeled', 'human', 'X', false)"
            ).format(self.api.table("safeact_principals")))


if __name__ == "__main__":
    unittest.main()
