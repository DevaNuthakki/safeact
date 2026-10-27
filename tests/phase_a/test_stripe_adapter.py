"""Stripe sandbox adapter, offline (no network, never Stripe).

The key loader and the sandbox gate; the adapter's classification table
against a fake that models Stripe's documented behaviour
(connectors/stripe_fake.py); the lookup's contract; refusal of live mode.
No test here, or anywhere in CI, sends a request to Stripe.
"""

import os
import pickle
import tempfile
import threading
import unittest

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

import httpx2 as httpx

from connectors.base import DispatchKind, LookupTarget
from connectors.stripe_fake import FakeStripe
from connectors.stripe_key import (
    StripeKey,
    StripeKeyRefused,
    StripeSandboxRefused,
    load_stripe_key,
    sandbox_allowed,
)
from connectors.stripe_payments import (
    LiveModeRefused,
    StripePaymentsConnector,
    classify,
)
from core.connector_profiles import (
    STRIPE,
    STRIPE_API_VERSION,
    stripe_profile,
)
from core.contracts import EvidenceKind, canonical_payload_hash
from core.provider_keys import NATIVE_KEY_CONNECTORS, derive_provider_key


TEST_KEY = "rk_test_" + "A1b2C3d4" * 4   # synthetic, never a real key
ROOT = Path(__file__).resolve().parents[2]


class KeyTests(unittest.TestCase):

    def test_only_a_restricted_test_key_is_accepted(self):
        self.assertEqual(StripeKey(TEST_KEY).reveal(), TEST_KEY)
        for value in (
            "sk_live_" + "x" * 24, "rk_live_" + "x" * 24,
            "pk_live_" + "x" * 24, "sk_test_" + "x" * 24,
            "pk_test_" + "x" * 24, "whsec_" + "x" * 24,
            "sk_org_" + "x" * 24, "rk_test_short", "rk_test_" + "x" * 20 + "!",
            "", " " + TEST_KEY,
        ):
            with self.subTest(kind=value[:8]):
                with self.assertRaises(StripeKeyRefused) as caught:
                    StripeKey(value)
                if value:
                    self.assertNotIn(value, str(caught.exception))
                    self.assertNotIn(value[8:], str(caught.exception))

    def test_the_value_never_shows(self):
        key = StripeKey(TEST_KEY)
        self.assertNotIn(TEST_KEY[8:], repr(key) + str(key) + f"{key}")
        with self.assertRaises(TypeError):
            pickle.dumps(key)

    def test_the_key_file_rules(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "stripe.key"
            env = {"SAFEACT_STRIPE_KEY_FILE": str(path)}
            with self.assertRaises(StripeKeyRefused):
                load_stripe_key(env)                       # missing
            path.write_text(TEST_KEY + "\n")
            path.chmod(0o644)
            with self.assertRaises(StripeKeyRefused):
                load_stripe_key(env)                       # group/other bits
            path.chmod(0o600)
            self.assertEqual(load_stripe_key(env).reveal(), TEST_KEY)
            link = Path(home) / "link.key"
            link.symlink_to(path)
            with self.assertRaises(StripeKeyRefused):
                load_stripe_key({"SAFEACT_STRIPE_KEY_FILE": str(link)})
            path.write_text("sk_live_" + "x" * 24)
            with self.assertRaises(StripeKeyRefused):
                load_stripe_key(env)
            with self.assertRaises(StripeKeyRefused):
                load_stripe_key(env, repo_root=home)      # inside the repo

    def test_the_sandbox_gate(self):
        self.assertFalse(sandbox_allowed({}))
        self.assertTrue(sandbox_allowed({"SAFEACT_ALLOW_STRIPE_SANDBOX": "1"}))
        for ci in ({"CI": "true"}, {"GITHUB_ACTIONS": "true"}):
            with self.subTest(ci=ci):
                self.assertFalse(sandbox_allowed(
                    {"SAFEACT_ALLOW_STRIPE_SANDBOX": "1", **ci}))

    def test_no_real_client_without_the_gate(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(StripeSandboxRefused):
                StripePaymentsConnector(StripeKey(TEST_KEY))
        with mock.patch.dict(os.environ, {"SAFEACT_ALLOW_STRIPE_SANDBOX": "1",
                                          "CI": "true"}, clear=True):
            with self.assertRaises(StripeSandboxRefused):
                StripePaymentsConnector(StripeKey(TEST_KEY))
        with mock.patch.dict(os.environ, {"SAFEACT_ALLOW_STRIPE_SANDBOX": "1"},
                             clear=True):
            with self.assertRaises(ValueError):
                StripePaymentsConnector(StripeKey(TEST_KEY),
                                        base_url="https://evil.example")


class ProfileTests(unittest.TestCase):

    def test_contract(self):
        profile = stripe_profile()
        native = profile.contract.native_idempotency
        self.assertEqual(profile.connector_id, STRIPE)
        self.assertEqual(native.retention, timedelta(hours=24))
        self.assertTrue(native.binds_parameters)
        self.assertTrue(native.protects_concurrent_same_key)
        self.assertEqual(profile.row()["lookup_semantics"], "eventual")
        self.assertIsNone(profile.compensation_action)
        self.assertIn(STRIPE_API_VERSION, profile.contract_reference)
        self.assertLessEqual(len(profile.contract_reference), 255)
        self.assertIn(STRIPE, NATIVE_KEY_CONNECTORS)
        self.assertTrue(derive_provider_key("t", "r", STRIPE, "acct")
                        .startswith("sa1_"))


class AdapterTests(unittest.TestCase):

    def setUp(self):
        self.fake = FakeStripe(TEST_KEY)
        self.requests = []
        inner = self.fake.transport()

        def capture(request):
            self.requests.append(request)
            return inner.handle_request(request)

        self.connector = StripePaymentsConnector(
            StripeKey(TEST_KEY), transport=httpx.MockTransport(capture),
            base_url="https://fake.stripe.test",
        )
        self.profile = stripe_profile()

    def intent(self, amount=500, total=2000, key=None, pi=None):
        pi = pi or self.fake.payment_intent(total)
        payload = {"payment_intent": pi, "amount_minor": amount}
        canonical = (
            '{"amount_minor":' + str(amount)
            + ',"payment_intent":"' + pi + '"}'
        )
        return SimpleNamespace(
            connector_id=STRIPE, action_type="refund_payment",
            provider_idempotency_key=key or "sa1_" + uuid4().hex,
            payload_canonical=canonical,
            payload_sha256=canonical_payload_hash(payload),
        )

    def dispatch(self, intent, operation_id=None):
        return self.connector.dispatch(
            intent, operation_id=operation_id or uuid4(), timeout=2,
        )

    def target(self, intent, operation_id, *, start=None):
        start = start or datetime.now(timezone.utc) - timedelta(seconds=5)
        return LookupTarget(
            operation_id=operation_id, provider_account_id="acct",
            provider_idempotency_key=intent.provider_idempotency_key,
            payload_sha256=intent.payload_sha256,
            first_dispatch_at=start, observed_at=start + timedelta(seconds=5),
        )

    def test_a_refund_is_applied_with_pinned_headers(self):
        intent = self.intent()
        result = self.dispatch(intent)
        self.assertEqual(result.kind, DispatchKind.APPLIED)
        self.assertTrue(result.reference.startswith("re_"))
        sent = self.requests[-1]
        self.assertEqual(sent.headers["idempotency-key"],
                         intent.provider_idempotency_key)
        self.assertEqual(sent.headers["stripe-version"], STRIPE_API_VERSION)
        self.assertEqual(sent.headers["authorization"], f"Bearer {TEST_KEY}")
        self.assertNotIn(TEST_KEY, str(sent.url))
        self.assertNotIn(TEST_KEY, sent.content.decode())

    def test_replay_and_changed_parameters(self):
        intent = self.intent()
        operation_id = uuid4()
        first = self.dispatch(intent, operation_id)
        again = self.dispatch(intent, operation_id)
        self.assertEqual(again.reference, first.reference)
        changed = self.intent(amount=600, key=intent.provider_idempotency_key,
                              pi=intent.payload_canonical.split('"')[-2])
        result = self.dispatch(changed, operation_id)
        self.assertEqual((result.kind, result.reason),
                         (DispatchKind.AMBIGUOUS, "ambiguous_key_conflict"))
        self.assertEqual(len(self.fake.ledger.snapshot()[1]), 1)

    def test_same_key_while_executing_is_in_flight(self):
        self.fake.hold = threading.Event()
        intent, operation_id = self.intent(), uuid4()
        results = []
        thread = threading.Thread(target=lambda: results.append(
            self.dispatch(intent, operation_id)))
        thread.start()
        for _ in range(100):
            if self.fake._executing:
                break
            threading.Event().wait(0.01)
        second = self.dispatch(intent, operation_id)
        self.fake.hold.set()
        thread.join()
        self.assertEqual((second.kind, second.reason),
                         (DispatchKind.AMBIGUOUS, "provider_in_flight_conflict"))
        self.assertEqual(results[0].kind, DispatchKind.APPLIED)
        self.assertEqual(len(self.fake.ledger.snapshot()[1]), 1)

    def test_rate_limits_and_lock_timeouts_are_retryable_rejections(self):
        for switch in ("rate_limit_next", "lock_timeout_next"):
            with self.subTest(fault=switch):
                setattr(self.fake, switch, True)
                result = self.dispatch(self.intent())
                self.assertEqual(result.kind, DispatchKind.NOT_APPLIED)
                self.assertTrue(result.retryable)
        self.assertEqual(self.fake.ledger.snapshot()[1], ())

    def test_server_error_after_effect_stays_ambiguous_and_is_found(self):
        intent, operation_id = self.intent(), uuid4()
        self.fake.server_error_after_effect = True
        result = self.dispatch(intent, operation_id)
        self.assertEqual((result.kind, result.reason),
                         (DispatchKind.AMBIGUOUS, "ambiguous_error_after_send"))
        # Stripe saves a 500: the same key replays it, never re-executes.
        self.assertEqual(self.dispatch(intent, operation_id).kind,
                         DispatchKind.AMBIGUOUS)
        self.assertEqual(len(self.fake.ledger.snapshot()[1]), 1)
        found = self.connector.lookup(self.target(intent, operation_id),
                                      self.profile)
        self.assertEqual(found.kind, EvidenceKind.APPLIED)

    def test_lost_response_is_no_response(self):
        intent, operation_id = self.intent(), uuid4()
        self.fake.drop_response_after_effect = True
        result = self.dispatch(intent, operation_id)
        self.assertEqual((result.kind, result.reason),
                         (DispatchKind.AMBIGUOUS, "ambiguous_no_response"))
        self.assertEqual(self.connector.lookup(
            self.target(intent, operation_id), self.profile).kind,
            EvidenceKind.APPLIED)

    def test_refused_refunds_are_not_applied(self):
        too_large = self.dispatch(self.intent(amount=5000, total=2000))
        self.assertEqual(too_large.kind, DispatchKind.NOT_APPLIED)
        self.assertIn("amount_too_large", too_large.reference)
        pi = self.fake.payment_intent(1000)
        self.assertIs(self.dispatch(self.intent(amount=1000, pi=pi)).kind,
                      DispatchKind.APPLIED)
        again = self.dispatch(self.intent(amount=100, pi=pi))
        self.assertEqual(again.kind, DispatchKind.NOT_APPLIED)
        self.assertIn("charge_already_refunded", again.reference)
        bad = self.intent()
        bad.payload_canonical = '{"amount_minor":-1}'
        self.assertEqual(self.dispatch(bad).reference, "local:invalid_payload")

    def test_the_refund_status_is_recorded_as_a_fact(self):
        intent, operation_id = self.intent(), uuid4()
        self.fake.refund_status = "pending"
        result = self.dispatch(intent, operation_id)
        self.assertEqual(result.kind, DispatchKind.APPLIED)
        self.assertEqual(dict(result.provider_facts),
                         {"refund_status": "pending"})
        found = self.connector.lookup(self.target(intent, operation_id),
                                      self.profile)
        self.assertEqual(dict(found.provider_facts),
                         {"refund_status": "pending"})
        odd = classify(httpx.Response(200, json={
            "object": "refund", "id": "re_2", "status": "<script>"}))
        self.assertEqual(dict(odd.provider_facts),
                         {"refund_status": "unreported"})

    def test_live_mode_stops_everything(self):
        self.fake.live_mode = True
        with self.assertRaises(LiveModeRefused):
            self.dispatch(self.intent())

    def test_lookup_absence_is_never_proof(self):
        intent, operation_id = self.intent(), uuid4()
        self.fake.hide_new_refunds = True
        self.dispatch(intent, operation_id)
        hidden = self.connector.lookup(self.target(intent, operation_id),
                                       self.profile)
        self.assertEqual(hidden.kind, EvidenceKind.NOT_FOUND)
        self.fake.reveal()
        self.assertEqual(self.connector.lookup(
            self.target(intent, operation_id), self.profile).kind,
            EvidenceKind.APPLIED)
        never = self.connector.lookup(self.target(self.intent(), uuid4()),
                                      self.profile)
        self.assertEqual(never.kind, EvidenceKind.NOT_FOUND)

    def test_lookup_checks_the_bound_parameters(self):
        intent, operation_id = self.intent(), uuid4()
        self.dispatch(intent, operation_id)
        other = self.intent(amount=999)
        found = self.connector.lookup(self.target(other, operation_id),
                                      self.profile)
        self.assertEqual(found.kind, EvidenceKind.INCONCLUSIVE)

    def test_lookup_pages_through_the_window(self):
        for _ in range(105):
            self.dispatch(self.intent(amount=1, total=1))
        intent, operation_id = self.intent(), uuid4()
        self.dispatch(intent, operation_id)
        for _ in range(105):
            self.dispatch(self.intent(amount=1, total=1))
        self.assertEqual(self.connector.lookup(
            self.target(intent, operation_id), self.profile).kind,
            EvidenceKind.APPLIED)

    def test_lookup_failures_are_inconclusive(self):
        intent, operation_id = self.intent(), uuid4()
        target = self.target(intent, operation_id)
        broken = StripePaymentsConnector(
            StripeKey(TEST_KEY), base_url="https://fake.stripe.test",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(500, json={})),
        )
        self.assertEqual(broken.lookup(target, self.profile).kind,
                         EvidenceKind.INCONCLUSIVE)
        undated = LookupTarget(operation_id, "acct", "k", "h", None, None)
        self.assertEqual(self.connector.lookup(undated, self.profile).kind,
                         EvidenceKind.INCONCLUSIVE)


class ClassificationTableTests(unittest.TestCase):

    def response(self, status, body=None, headers=None):
        return httpx.Response(status, json=body or {}, headers=headers or {})

    def test_table(self):
        error = lambda kind, code=None: {  # noqa: E731
            "error": {"type": kind, **({"code": code} if code else {})}}
        cases = (
            (self.response(200, {"object": "refund", "id": "re_1"}),
             DispatchKind.APPLIED, False),
            (self.response(200, {"object": "charge", "id": "ch_1"}),
             DispatchKind.AMBIGUOUS, False),
            (self.response(401, error("invalid_request_error")),
             DispatchKind.NOT_APPLIED, False),
            (self.response(403, error("invalid_request_error")),
             DispatchKind.NOT_APPLIED, False),
            (self.response(400, error("idempotency_error")),
             DispatchKind.AMBIGUOUS, False),
            (self.response(400, error("invalid_request_error",
                                      "parameter_missing")),
             DispatchKind.NOT_APPLIED, False),
            (self.response(400, error("invalid_request_error", "other")),
             DispatchKind.AMBIGUOUS, False),
            (self.response(402, error("card_error", "card_declined")),
             DispatchKind.AMBIGUOUS, False),
            (self.response(409, error("idempotency_error")),
             DispatchKind.AMBIGUOUS, False),
            (self.response(429, error("invalid_request_error", "rate_limit"),
                           {"Stripe-Rate-Limited-Reason": "endpoint-rate"}),
             DispatchKind.NOT_APPLIED, True),
            (self.response(429, error("invalid_request_error")),
             DispatchKind.AMBIGUOUS, False),
            (self.response(500), DispatchKind.AMBIGUOUS, False),
            (self.response(503), DispatchKind.AMBIGUOUS, False),
            (self.response(418), DispatchKind.AMBIGUOUS, False),
        )
        for response, kind, retryable in cases:
            with self.subTest(status=response.status_code,
                              body=response.content[:60]):
                result = classify(response)
                self.assertEqual((result.kind, result.retryable),
                                 (kind, retryable))


class SecretShapeTests(unittest.TestCase):

    def test_logs_and_scans_catch_stripe_shapes(self):
        from api.logging import redact_text
        from deploy.evaluation.agent_v1.secret_scan import scan_text

        for value in (TEST_KEY, "sk_live_" + "Z9" * 12, "rk_live_" + "Z9" * 12,
                      "pk_test_" + "Z9" * 12, "whsec_" + "Z9" * 12):
            with self.subTest(kind=value[:7]):
                self.assertNotIn(value, redact_text(f"auth {value} end"))
                self.assertTrue(scan_text(f"leak: {value}\n"))
        self.assertFalse(scan_text("refund re_1Nispe2eZvKYlo2Cd31jOCgZ ok\n"))

    def test_the_adapter_never_reads_the_ledger_or_logs_the_key(self):
        text = (ROOT / "connectors/stripe_payments.py").read_text()
        self.assertNotIn("ledger", text.lower().replace(
            "the test ledger plays no part", ""))
        self.assertNotIn("print(", text)
        self.assertNotIn("logging", text)


if __name__ == "__main__":
    unittest.main()
