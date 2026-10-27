"""Metrics and structured logs from the separate-process stack (M5.7).

The stack runs with an exporter per process. After a refund through the
API, each exporter is scraped:
- the worker (prefork, multiprocess mode) counts the applied dispatch and
  its latency, by connector;
- the reconciler exports the durable gauges read from PostgreSQL
  (operations per state, claimable backlog);
- the API counts requests by route template, never by raw path.
No exported label value carries an operation id, a tenant or a key. The
JSON logs carry operation ids for correlation, and no secret: neither the
role passwords nor anything shaped like a key or token.
Redis DB 9 only.
"""

import os
import re
import tempfile
import time
import unittest
import uuid

from pathlib import Path

import httpx2
import psycopg
import redis

from api.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from deploy.demo.bootstrap import (
    DEMO_ACCOUNTS, Environment, credential_lines, rebuild,
    teardown_statements, write_credentials,
)
from deploy.demo.profiles import E2E_PROFILES
from deploy.evaluation.agent_v1.secret_scan import scan
from deploy.stack.processes import ProcessStack
from storage.database import local_admin_settings, local_redis_url
from tests.integration.test_stack_processes import free_port


ENABLED = (
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1"
    and os.environ.get("SAFEACT_RUN_BROKER_TESTS") == "1"
)
REDIS_DB = 9
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def samples(text, name):
    """{labels-string: value} for one metric name in exposition text."""
    found = {}
    for line in text.splitlines():
        if line.startswith(name) and not line.startswith("#"):
            key, _, value = line.rpartition(" ")
            found[key] = float(value)
    return found


@unittest.skipUnless(ENABLED, "Observability stack tests are opt-in")
class ObservabilityStackTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.redis = redis.Redis.from_url(local_redis_url(REDIS_DB))
        cls.redis.flushdb()
        cls.addClassCleanup(cls.redis.flushdb)
        cls.admin = psycopg.connect(**local_admin_settings(), autocommit=True)
        cls.env = Environment.disposable(uuid.uuid4().hex)
        cls.addClassCleanup(cls.teardown_env)
        cls.role_passwords, cls.passwords = rebuild(
            cls.admin, cls.env, profiles=E2E_PROFILES, accounts=DEMO_ACCOUNTS)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        credentials = Path(cls.tmp.name) / "credentials"
        write_credentials(credentials, credential_lines(cls.env,
                                                        cls.role_passwords))
        cls.ports = {c: free_port() for c in ("worker", "reconciler", "api")}
        cls.logs = Path(cls.tmp.name) / "logs"
        cls.stack = ProcessStack(
            credentials=credentials, redis_db=REDIS_DB,
            queue="safeact-obs-" + uuid.uuid4().hex[:8], api_port=free_port(),
            provider_ports={k: free_port()
                            for k in ("payments", "crm", "email", "admin")},
            poll_seconds=0.2, concurrency=2,
            components=("providers", "worker", "relay", "reconciler", "api"),
            log_dir=cls.logs, metrics_ports=cls.ports,
        )
        cls.addClassCleanup(cls.stack.stop)
        cls.stack.start()

        http = httpx2.Client(base_url=cls.stack.base_url, timeout=10)
        cls.addClassCleanup(http.close)
        response = http.post("/auth/login", json={
            "username": "demo-north-requester",
            "password": cls.passwords["demo-north-requester"]})
        response.raise_for_status()
        for name in (SESSION_COOKIE, CSRF_COOKIE):
            http.cookies.set(name, response.cookies[name])
        http.headers[CSRF_HEADER] = response.json()["csrf_token"]
        created = http.post("/actions", json={
            "request_key": "rk-" + uuid.uuid4().hex[:12],
            "action_type": "refund_payment",
            "provider_account_id": "mock-payments-account",
            "payload": {"charge_id": "ch_north_1001", "amount_minor": 1500},
        })
        assert created.status_code in (200, 201), created.text
        cls.operation_id = created.json()["operation_id"]
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            detail = http.get(f"/actions/{cls.operation_id}").json()
            if detail["state"] == "SUCCEEDED":
                break
            time.sleep(0.2)
        cls.detail = detail

    @classmethod
    def teardown_env(cls):
        cls.admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE usename = ANY(%s)", (list(cls.env.roles),))
        with cls.admin.transaction():
            for statement in teardown_statements(cls.admin, cls.env):
                cls.admin.execute(statement)
        cls.admin.close()

    def scrape(self, component):
        response = httpx2.get(
            f"http://127.0.0.1:{self.ports[component]}/metrics", timeout=5)
        self.assertEqual(response.status_code, 200)
        return response.text

    def test_the_refund_succeeded(self):
        self.assertEqual(self.detail["state"], "SUCCEEDED")

    def test_the_worker_counts_the_dispatch(self):
        text = self.scrape("worker")
        outcomes = samples(text, "safeact_dispatch_outcomes_total")
        applied = [v for k, v in outcomes.items()
                   if 'connector="mock-payments"' in k and 'kind="applied"' in k]
        self.assertTrue(applied and applied[0] >= 1, outcomes)
        self.assertTrue(samples(text, "safeact_dispatch_seconds_count"))

    def test_the_reconciler_exports_durable_gauges(self):
        text = self.scrape("reconciler")
        operations = samples(text, "safeact_operations")
        self.assertEqual(operations.get('safeact_operations{state="SUCCEEDED"}'),
                         1.0, operations)
        self.assertIn("safeact_claimable", text)
        self.assertIn("safeact_breakers_open", text)

    def test_the_api_counts_by_route_template(self):
        text = self.scrape("api")
        self.assertIn('route="/actions/{operation_id}"', text)

    def test_no_label_carries_an_identifier(self):
        for component in self.ports:
            with self.subTest(component=component):
                text = self.scrape(component)
                for line in text.splitlines():
                    if line.startswith("#") or "{" not in line:
                        continue
                    self.assertIsNone(UUID.search(line), line)
                    self.assertNotIn("north", line)
                    self.assertNotIn("sa1_", line)

    def test_logs_are_json_correlated_and_secret_free(self):
        import json

        worker = (self.logs / "worker.log").read_text(errors="replace")
        events = [json.loads(line) for line in worker.splitlines()
                  if line.startswith("{")]
        dispatch = [e for e in events if e.get("message") == "dispatch_outcome"]
        self.assertTrue(any(e.get("operation_id") == self.operation_id
                            for e in dispatch), dispatch[:3])
        everything = "".join(p.read_text(errors="replace")
                             for p in self.logs.glob("*.log"))
        for password in list(self.role_passwords.values()) + list(
                self.passwords.values()):
            self.assertNotIn(password, everything)
        self.assertEqual(scan([self.logs]), {})


if __name__ == "__main__":
    unittest.main()
