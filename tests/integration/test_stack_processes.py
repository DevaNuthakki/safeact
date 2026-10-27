"""The stack as separate OS processes over HTTP mock providers.

Every component runs in its own process with its own database role: the
mock providers (HTTP), a real Celery prefork worker, the relay, the
reconciler, the advancer and the API. Effects are counted from the mock
providers' independent ledger through the harness-only admin port.

Asserted:
- a refund submitted through the API reaches SUCCEEDED with exactly one
  provider effect;
- timeout after success over a real HTTP connection (the provider
  applies the refund, then holds the response until the worker's
  request deadline passes): the reconciler verifies it by lookup and it
  succeeds with one effect and one provider call, never a second
  dispatch;
- the worker really is a separate process tree from the test.

Redis DB 11 only; flushed before and after. Opt-in with
SAFEACT_RUN_DB_INTEGRATION=1, SAFEACT_RUN_DISPOSABLE_DB_TESTS=1 and
SAFEACT_RUN_BROKER_TESTS=1.
"""

import os
import socket
import tempfile
import time
import unittest
import uuid

from pathlib import Path

import httpx2
import psycopg
import redis

from psycopg import sql

from api.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from deploy.demo.bootstrap import (
    DEMO_ACCOUNTS, Environment, credential_lines, rebuild,
    teardown_statements, write_credentials,
)
from deploy.demo.profiles import E2E_PROFILES
from deploy.stack.processes import ProcessStack
from storage.database import local_admin_settings, local_redis_url
from storage.state_projection import projection_drift


ENABLED = (
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1"
    and os.environ.get("SAFEACT_RUN_BROKER_TESTS") == "1"
)
REDIS_DB = 11


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@unittest.skipUnless(ENABLED, "Process stack tests are opt-in")
class ProcessStackTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.redis = redis.Redis.from_url(local_redis_url(REDIS_DB))
        cls.redis.flushdb()
        cls.addClassCleanup(cls.redis.flushdb)

        cls.admin = psycopg.connect(**local_admin_settings(), autocommit=True)
        cls.env = Environment.disposable(uuid.uuid4().hex)
        cls.addClassCleanup(cls.teardown_env)
        role_passwords, cls.passwords = rebuild(
            cls.admin, cls.env, profiles=E2E_PROFILES, accounts=DEMO_ACCOUNTS,
        )

        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        credentials = Path(cls.tmp.name) / "stack-credentials"
        write_credentials(credentials, credential_lines(
            cls.env, role_passwords,
        ))

        cls.stack = ProcessStack(
            credentials=credentials, redis_db=REDIS_DB,
            queue="safeact-it-" + uuid.uuid4().hex[:8],
            api_port=free_port(),
            provider_ports={k: free_port()
                            for k in ("payments", "crm", "email", "admin")},
            poll_seconds=0.2, concurrency=2,
            components=("providers", "worker", "relay", "reconciler",
                        "advancer", "api"),
            log_dir=Path(cls.tmp.name) / "logs",
        )
        cls.addClassCleanup(cls.stack.stop)
        cls.stack.start()
        cls.ledger = httpx2.Client(base_url=cls.stack.admin_url, timeout=5)
        cls.addClassCleanup(cls.ledger.close)
        cls.http = cls.sign_in("demo-north-requester")
        cls.addClassCleanup(cls.http.close)

    @classmethod
    def teardown_env(cls):
        drift = projection_drift(cls.admin, cls.env.schema)
        cls.admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE usename = ANY(%s)", (list(cls.env.roles),),
        )
        with cls.admin.transaction():
            for statement in teardown_statements(cls.admin, cls.env):
                cls.admin.execute(statement)
        cls.admin.close()
        if drift:
            raise AssertionError(f"Current-state projection drift: {drift[:3]}")

    @classmethod
    def sign_in(cls, username):
        http = httpx2.Client(base_url=cls.stack.base_url, timeout=10)
        response = http.post("/auth/login", json={
            "username": username, "password": cls.passwords[username],
        })
        response.raise_for_status()
        for name in (SESSION_COOKIE, CSRF_COOKIE):
            http.cookies.set(name, response.cookies[name])
        http.headers[CSRF_HEADER] = response.json()["csrf_token"]
        return http

    # -- helpers -------------------------------------------------------

    def refund(self, charge):
        response = self.http.post("/actions", json={
            "request_key": "rk-" + uuid.uuid4().hex[:16],
            "action_type": "refund_payment",
            "provider_account_id": "mock-payments-account",
            "payload": {"charge_id": charge, "amount_minor": 1500},
        })
        self.assertIn(response.status_code, (200, 201), response.text)
        return response.json()["operation_id"]

    def wait_for(self, operation_id, states, timeout=40):
        deadline = time.monotonic() + timeout
        state = None
        while time.monotonic() < deadline:
            response = self.http.get(f"/actions/{operation_id}")
            self.assertEqual(response.status_code, 200, response.text)
            state = response.json()["state"]
            if state in states:
                return state
            time.sleep(0.2)
        self.fail(f"{operation_id} stayed {state}, wanted {states}")

    def provider_key(self, operation_id):
        return self.admin.execute(sql.SQL(
            "SELECT provider_idempotency_key FROM {}.safeact_operations "
            "WHERE operation_id = %s"
        ).format(sql.Identifier(self.env.schema)),
            (operation_id,),
        ).fetchone()[0]

    def ledger_for(self, key):
        effects = self.ledger.get("/ledger/effects",
                                  params={"correlation": key}).json()
        calls = self.ledger.get("/ledger/calls",
                                params={"correlation": key}).json()
        return len(effects["effects"]), len(calls["calls"])

    # -- tests -----------------------------------------------------------

    def test_a_refund_succeeds_with_one_effect(self):
        operation_id = self.refund("ch_north_1001")
        self.assertEqual(self.wait_for(operation_id, {"SUCCEEDED"}),
                         "SUCCEEDED")
        self.assertEqual(self.ledger_for(self.provider_key(operation_id)),
                         (1, 1))

    def test_timeout_after_success_over_http_is_verified_not_resent(self):
        # The demo fault script: the provider applies the refund, then
        # holds the response past the worker's 1 s request deadline.
        operation_id = self.refund("ch_north_1002")
        self.assertEqual(self.wait_for(operation_id, {"SUCCEEDED"}),
                         "SUCCEEDED")
        detail = self.http.get(f"/actions/{operation_id}").json()
        reasons = [t["reason"] for t in detail["transitions"]]
        self.assertIn("ambiguous_no_response", reasons)
        self.assertEqual(self.ledger_for(self.provider_key(operation_id)),
                         (1, 1))

    def test_components_are_separate_processes(self):
        pids = {c: p.pid for c, p in self.stack.processes.items()}
        self.assertEqual(len(set(pids.values())), len(pids))
        self.assertNotIn(os.getpid(), pids.values())
        for component, process in self.stack.processes.items():
            with self.subTest(component=component):
                self.assertIsNone(process.poll())


if __name__ == "__main__":
    unittest.main()
