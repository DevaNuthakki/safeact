"""The optional Stripe test-mode demo profile (deploy/demo/stripe_demo.py).

Offline: the real adapter over connectors/stripe_fake.py, never Stripe. The
gates are checked before any key is read: SAFEACT_ALLOW_STRIPE_SANDBOX=1,
never under CI, and a live-mode object stops the stack.
"""

import json
import os
import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from api import catalog
from api.actions import SubmitAction
from connectors.base import DispatchKind, DispatchResult
from connectors.stripe_fake import FakeStripe
from connectors.stripe_key import StripeKey, StripeSandboxRefused
from connectors.stripe_payments import LiveModeRefused
from deploy.demo import stripe_demo
from deploy.demo.bootstrap import DEMO_ACCOUNTS, stripe_demo_accounts
from mcp_server.tools import TOOLS


FAKE_KEY = "rk_test_" + "OfflineFakeOnly" * 2
FIXTURES = {"normal": "pi_" + "a" * 24, "response_lost": "pi_" + "b" * 24}


class Inner:
    connector_id = "stripe-test"

    def __init__(self, result=None, error=None):
        self.result = result or DispatchResult(DispatchKind.APPLIED, "provider_confirmed", "re_1")
        self.error = error
        self.calls = 0

    def accepts(self, intent):
        return True

    def dispatch(self, intent, *, operation_id, timeout=None):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    def lookup(self, target, profile):
        if self.error:
            raise self.error
        return "looked-up"


def intent(payment_intent, key="sa1_key"):
    return SimpleNamespace(provider_idempotency_key=key, payload_canonical=json.dumps(
        {"amount_minor": 700, "payment_intent": payment_intent}))


class StripeDemoTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_main_refuses_without_the_explicit_flag_or_under_ci_before_reading_a_key(self):
        for env in ({}, {"SAFEACT_ALLOW_STRIPE_SANDBOX": "1", "CI": "true"},
                    {"SAFEACT_ALLOW_STRIPE_SANDBOX": "1", "GITHUB_ACTIONS": "true"}):
            for command in ("setup", "check"):
                with self.subTest(env=env, command=command), \
                        mock.patch.dict(os.environ, env, clear=True), \
                        mock.patch("deploy.demo.stripe_demo.load_stripe_key") as load:
                    with self.assertRaises(StripeSandboxRefused):
                        stripe_demo.main([command, "--fixtures", str(self.dir / "f.json")])
                    load.assert_not_called()
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(StripeSandboxRefused):
                stripe_demo.demo_connector(FIXTURES, key=StripeKey(FAKE_KEY))
            with self.assertRaises(StripeSandboxRefused):
                stripe_demo.client(StripeKey(FAKE_KEY))

    def test_fixtures_are_private_and_hold_two_distinct_payment_intents(self):
        path = self.dir / "fixtures.json"
        with self.assertRaisesRegex(RuntimeError, "No Stripe demo fixtures"):
            stripe_demo.read_fixtures(path)
        stripe_demo.write_fixtures(path, FIXTURES)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(stripe_demo.read_fixtures(path), FIXTURES)
        path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, "permissions"):
            stripe_demo.read_fixtures(path)
        for bad in ({"normal": "ch_1"}, {**FIXTURES, "normal": "pi_bad id"},
                    {**FIXTURES, "normal": FIXTURES["response_lost"]}):
            stripe_demo.write_fixtures(path, bad)
            with self.subTest(bad=bad), self.assertRaisesRegex(RuntimeError, "Invalid"):
                stripe_demo.read_fixtures(path)

    def test_only_the_north_requester_gets_exactly_the_two_intents(self):
        grants = stripe_demo.grants(FIXTURES)
        self.assertEqual(grants, tuple(
            ("stripe-test", stripe_demo.ACCOUNT, "refund_payment", FIXTURES[case], "v1-demo")
            for case in stripe_demo.CASES))
        accounts = {a.username: a for a in stripe_demo_accounts(FIXTURES)}
        original = {a.username: a for a in DEMO_ACCOUNTS}
        for name, account in accounts.items():
            extra = set(account.grants) - set(original[name].grants)
            self.assertEqual(extra, set(grants) if name == "demo-north-requester" else set())
        # Stripe's documented key retention is unchanged; only demo windows shrink.
        retention = stripe_demo.PROFILE.contract.native_idempotency.retention
        self.assertEqual(retention.total_seconds(), 24 * 3600)

    def test_only_the_first_response_for_the_chosen_intent_is_dropped(self):
        inner = Inner()
        wrapper = stripe_demo.DropFirstResponse(inner, FIXTURES["response_lost"])
        normal = wrapper.dispatch(intent(FIXTURES["normal"], "k1"), operation_id="o1")
        self.assertIs(normal.kind, DispatchKind.APPLIED)
        lost = wrapper.dispatch(intent(FIXTURES["response_lost"], "k2"), operation_id="o2")
        self.assertIs(lost.kind, DispatchKind.AMBIGUOUS)
        self.assertEqual(lost.reason, "ambiguous_no_response")
        self.assertEqual(wrapper.dropped, {"k2": "applied"})
        # Stripe was called each time; a later request is answered honestly.
        again = wrapper.dispatch(intent(FIXTURES["response_lost"], "k2"), operation_id="o2")
        self.assertIs(again.kind, DispatchKind.APPLIED)
        self.assertEqual(inner.calls, 3)
        self.assertEqual(wrapper.lookup("target", "profile"), "looked-up")
        self.assertEqual(wrapper.connector_id, "stripe-test")

    def test_a_live_mode_object_stops_the_stack_and_is_re_raised(self):
        stopped = []
        wrapper = stripe_demo.DropFirstResponse(
            Inner(error=LiveModeRefused("live")), FIXTURES["response_lost"],
            on_live=lambda: stopped.append(True))
        with self.assertRaises(LiveModeRefused):
            wrapper.dispatch(intent(FIXTURES["normal"]), operation_id="o1")
        with self.assertRaises(LiveModeRefused):
            wrapper.lookup("target", "profile")
        self.assertEqual(stopped, [True, True])

    def test_the_console_action_never_reaches_agents(self):
        spec = catalog.CONSOLE_ACTIONS["refund_stripe_test_payment"]
        self.assertEqual((spec.connector_id, spec.action_type, spec.resource_field),
                         ("stripe-test", "refund_payment", "payment_intent"))
        self.assertNotIn("refund_stripe_test_payment", catalog.ACTIONS)
        self.assertFalse(any(s.connector_id == "stripe-test" for s in catalog.ACTIONS.values()))
        enum = TOOLS["submit_action"]["input_schema"]["properties"]["action"]["enum"]
        self.assertNotIn("refund_stripe_test_payment", enum)
        SubmitAction(request_key="rk-1234567890", action_type="refund_stripe_test_payment",
                     provider_account_id=stripe_demo.ACCOUNT, payload={})
        payload = spec.payload.model_validate({"payment_intent": FIXTURES["normal"],
                                               "amount_minor": 700})
        self.assertEqual(json.loads(catalog.canonical(payload)),
                         {"amount_minor": 700, "payment_intent": FIXTURES["normal"]})
        for bad in ({"payment_intent": "ch_1", "amount_minor": 700},
                    {"payment_intent": FIXTURES["normal"], "amount_minor": 0}):
            with self.assertRaises(ValueError):
                spec.payload.model_validate(bad)

    def test_setup_and_check_against_the_fake(self):
        fake = FakeStripe(FAKE_KEY)
        path = self.dir / "fixtures.json"
        calls = self.dir / "calls.jsonl"
        options = dict(key=StripeKey(FAKE_KEY), transport=fake.transport(),
                       base_url="https://fake.stripe.test", call_log=calls)
        fixtures = stripe_demo.setup(path, **options)
        self.assertEqual(stripe_demo.read_fixtures(path)["normal"], fixtures["normal"])
        report, ok = stripe_demo.check(path, **options)
        self.assertFalse(ok)
        self.assertEqual(report, {"normal": [], "response_lost": []})
        connector = stripe_demo.demo_connector(fixtures, **options)
        self.assertIsNotNone(connector)
        with stripe_demo.client(**options) as http:
            for case in stripe_demo.CASES:
                http.post("/v1/refunds", data={
                    "payment_intent": fixtures[case], "amount": "500",
                    "metadata[safeact_operation_id]": case},
                    headers={"Idempotency-Key": "sa1_" + case})
        report, ok = stripe_demo.check(path, **options)
        self.assertTrue(ok)
        self.assertEqual({case: [r["operation"] for r in found] for case, found in report.items()},
                         {"normal": ["normal"], "response_lost": ["response_lost"]})
        fake.live_mode = True
        with stripe_demo.client(**options) as http:
            http.post("/v1/refunds", data={"payment_intent": fixtures["normal"],
                                           "amount": "100"})
        with self.assertRaises(LiveModeRefused):
            stripe_demo.check(path, **options)
        # The call log keeps request metadata only: never the key or headers.
        entries = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertEqual(calls.stat().st_mode & 0o777, 0o600)
        self.assertEqual({e["path"] for e in entries},
                         {"/v1/payment_intents", "/v1/refunds"})
        self.assertTrue(all(set(e) <= {"at", "method", "path", "key_tag", "status",
                                       "request_id", "replayed", "error", "latency_ms"}
                            for e in entries))
        self.assertNotIn("OfflineFakeOnly", calls.read_text())
        self.assertIn("sa1_normal"[4:20], {e["key_tag"] for e in entries})


if __name__ == "__main__":
    unittest.main()
