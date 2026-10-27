"""The Stripe test-mode demo profile over real HTTP, against the Stripe fake.

Opt-in: SAFEACT_RUN_API_E2E=1. The in-process stack (API, relay, Celery
worker on Redis database 14, reconciler) runs with the demo profile's
connector exactly as ``run_stack --stripe`` builds it: the real adapter
inside the dropped-response wrapper. Only the transport differs: it is
connectors/stripe_fake.py (documented Stripe behaviour, offline), so this
test never calls Stripe and runs in CI.

Asserted through the API and the fake's own refund records:
- a normal refund is SUCCEEDED from Stripe's answer, one refund;
- for the response_lost PaymentIntent the refund is applied, its response
  dropped; SafeAct records UNKNOWN, verifies by lookup and ends SUCCEEDED
  with one refund at the fake and one POST, never a second dispatch;
- the console action is admitted as refund_payment on stripe-test, and a
  PaymentIntent outside the exact grants is refused.
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

from api.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from connectors.stripe_fake import FakeStripe
from connectors.stripe_key import StripeKey
from deploy.demo import stripe_demo
from deploy.demo.bootstrap import (
    Environment, rebuild, stripe_demo_accounts, teardown_statements,
)
from deploy.demo.profiles import E2E_PROFILES
from deploy.demo.run_stack import Stack
from storage.database import local_admin_settings, local_redis_url
from storage.state_projection import projection_drift


ENABLED = os.environ.get("SAFEACT_RUN_API_E2E") == "1"
REDIS_DB = 14
FAKE_KEY = "rk_test_" + "OfflineFakeOnly" * 2
REQUESTER = "demo-north-requester"


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class CountingTransport(httpx2.BaseTransport):
    """Counts refund POSTs per idempotency key on the way to the fake."""

    def __init__(self, inner):
        self._inner = inner
        self.posts = {}

    def handle_request(self, request):
        if request.method == "POST" and request.url.path == "/v1/refunds":
            key = request.headers.get("idempotency-key")
            self.posts[key] = self.posts.get(key, 0) + 1
        return self._inner.handle_request(request)


@unittest.skipUnless(ENABLED, "API end-to-end tests are opt-in")
class StripeDemoProfileTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.redis = redis.Redis.from_url(local_redis_url(REDIS_DB))
        cls.redis.flushdb()
        cls.addClassCleanup(cls.redis.flushdb)

        cls.fake = FakeStripe(FAKE_KEY)
        cls.fixtures = {case: cls.fake.payment_intent(stripe_demo.CHARGE_MINOR)
                        for case in stripe_demo.CASES}
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        path = Path(tmp.name) / "fixtures.json"
        stripe_demo.write_fixtures(path, cls.fixtures)
        fixtures = stripe_demo.read_fixtures(path)

        cls.admin = psycopg.connect(**local_admin_settings(), autocommit=True)
        cls.env = Environment.disposable(uuid.uuid4().hex)
        cls.addClassCleanup(cls.teardown_env)
        role_passwords, cls.passwords = rebuild(
            cls.admin, cls.env, profiles=E2E_PROFILES + (stripe_demo.PROFILE,),
            accounts=stripe_demo_accounts(fixtures),
        )
        cls.transport = CountingTransport(cls.fake.transport())
        cls.connector = stripe_demo.demo_connector(
            fixtures, key=StripeKey(FAKE_KEY), transport=cls.transport,
            base_url="https://fake.stripe.test", call_log=None)
        roles = {"api": cls.env.api, "worker": cls.env.worker,
                 "recoverer": cls.env.recoverer, "advancer": cls.env.advancer}
        cls.stack = Stack(
            schema=cls.env.schema, roles=roles, passwords=role_passwords,
            port=free_port(), redis_db=REDIS_DB, poll_seconds=0.2,
            extra_connectors=(cls.connector,),
        ).start()
        cls.addClassCleanup(cls.stack.stop)

        cls.http = httpx2.Client(base_url=cls.stack.base_url, timeout=10)
        cls.addClassCleanup(cls.http.close)
        response = cls.http.post("/auth/login", json={
            "username": REQUESTER, "password": cls.passwords[REQUESTER]})
        response.raise_for_status()
        for name in (SESSION_COOKIE, CSRF_COOKIE):
            cls.http.cookies.set(name, response.cookies[name])
        cls.http.headers[CSRF_HEADER] = response.json()["csrf_token"]

    @classmethod
    def teardown_env(cls):
        drift = projection_drift(cls.admin, cls.env.schema)
        cls.admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE usename = ANY(%s)", (list(cls.env.roles),))
        with cls.admin.transaction():
            for statement in teardown_statements(cls.admin, cls.env):
                cls.admin.execute(statement)
        cls.admin.close()
        if drift:
            raise AssertionError(f"Current-state projection drift: {drift[:3]}")

    def refund(self, intent, amount=700):
        return self.http.post("/actions", json={
            "request_key": "rk-" + uuid.uuid4().hex[:16],
            "action_type": "refund_stripe_test_payment",
            "provider_account_id": stripe_demo.ACCOUNT,
            "contract_version": stripe_demo.PROFILE.version,
            "payload": {"payment_intent": intent, "amount_minor": amount},
        })

    def wait_state(self, operation_id, state, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            detail = self.http.get(f"/actions/{operation_id}").json()
            if detail["state"] == state:
                return detail
            time.sleep(0.25)
        self.fail(f"{operation_id} never reached {state}: {detail['state']}")

    def provider_key(self, operation_id):
        return self.admin.execute(
            f'SELECT provider_idempotency_key FROM "{self.env.schema}".safeact_operations '
            "WHERE operation_id = %s", (operation_id,)).fetchone()[0]

    def test_grants_offer_the_two_intents_as_refund_payment_on_stripe_test(self):
        grants = [g for g in self.http.get("/grants").json()
                  if g["connector_id"] == "stripe-test"]
        self.assertEqual(
            sorted((g["action_type"], g["resource_id"], g["contract_version"]) for g in grants),
            sorted(("refund_payment", self.fixtures[case], stripe_demo.PROFILE.version)
                   for case in stripe_demo.CASES))

    def test_normal_refund_succeeds_from_stripes_answer(self):
        response = self.refund(self.fixtures["normal"])
        self.assertEqual(response.status_code, 201, response.text)
        operation_id = response.json()["operation_id"]
        detail = self.wait_state(operation_id, "SUCCEEDED")
        self.assertEqual(detail["action_type"], "refund_payment")
        self.assertEqual(detail["connector_id"], "stripe-test")
        self.assertNotIn("UNKNOWN", [t["to_state"] for t in detail["transitions"]])
        self.assertEqual(len(self.fake.refunds_for(self.fixtures["normal"])), 1)
        self.assertEqual(self.transport.posts[self.provider_key(operation_id)], 1)

    def test_lost_response_is_verified_by_lookup_without_a_second_dispatch(self):
        intent = self.fixtures["response_lost"]
        response = self.refund(intent)
        self.assertEqual(response.status_code, 201, response.text)
        operation_id = response.json()["operation_id"]
        detail = self.wait_state(operation_id, "SUCCEEDED", timeout=90)
        states = [t["to_state"] for t in detail["transitions"]]
        self.assertIn("UNKNOWN", states)
        self.assertIn("VERIFYING", states)
        self.assertEqual(len(detail["attempts"]), 1)
        refunds = self.fake.refunds_for(intent)
        self.assertEqual(len(refunds), 1)
        self.assertEqual(refunds[0]["metadata"]["safeact_operation_id"], operation_id)
        key = self.provider_key(operation_id)
        self.assertEqual(self.transport.posts[key], 1)
        self.assertEqual(self.connector.dropped, {key: "applied"})

    def test_an_intent_outside_the_grants_is_refused(self):
        other = self.fake.payment_intent(1000)
        response = self.refund(other)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.fake.refunds_for(other), [])


if __name__ == "__main__":
    unittest.main()
