"""The HTTP mock providers' wire contract, both ends, without a network.

The FastAPI apps from connectors/http_mock serve the real in-process
mocks; the HTTP client facades talk to them through Starlette's
in-memory transport and are wrapped by the unchanged connectors. So
these tests check that an exchange over the wire is classified exactly
as the in-process call was. Real sockets, real timeouts and stalls are
covered by tests/integration/test_stack_processes.py.
"""

import unittest
import warnings

import httpx2 as httpx

from fastapi.testclient import TestClient

from connectors.base import DispatchKind
from connectors.crm_mock import (
    CrmSearchUnavailable,
    CrmServerError,
    CrmValidationRejected,
    MockCrmProvider,
)
from connectors.email_mock import EmailRejected, MockEmailProvider
from connectors.http_mock.admin_api import FAULTS, admin_app
from connectors.http_mock.provider_api import (
    Stall,
    crm_app,
    email_app,
    payments_app,
)
from connectors.http_providers import (
    HttpCrmProvider,
    HttpEmailProvider,
    HttpPaymentsProvider,
)
from connectors.ledger import EffectsLedger
from connectors.payments_mock import (
    IdempotencyConflict,
    LookupUnavailable,
    MockPaymentsProvider,
    ProviderRejected,
    ProviderServerError,
    ProviderTimeout,
)


class QuietTestClient(unittest.TestCase):
    """The facades always pass a timeout; the in-memory client warns."""

    def setUp(self):
        context = warnings.catch_warnings()
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        warnings.filterwarnings(
            "ignore", message="You should not use the 'timeout' argument",
        )


def mock_transport(handler):
    return httpx.Client(base_url="http://mock",
                        transport=httpx.MockTransport(handler))


class PaymentsWireTests(QuietTestClient):

    def setUp(self):
        super().setUp()
        self.ledger = EffectsLedger()
        self.mock = MockPaymentsProvider(ledger=self.ledger)
        self.http = TestClient(payments_app(self.mock, Stall(0)),
                               base_url="http://mock")
        self.provider = HttpPaymentsProvider("http://mock", client=self.http)

    def test_refund_replay_and_conflict(self):
        first = self.provider.refund("k1", {"amount": 5})
        again = self.provider.refund("k1", {"amount": 5})
        self.assertEqual((first.refund_id, first.replayed),
                         (again.refund_id, False))
        self.assertTrue(again.replayed)
        with self.assertRaises(IdempotencyConflict):
            self.provider.refund("k1", {"amount": 6})
        self.assertEqual(len(self.ledger.effects_for("k1")), 1)

    def test_rejection_keeps_reference_and_retryability(self):
        self.mock.reject_next_retryable = "rate-hold"
        with self.assertRaises(ProviderRejected) as caught:
            self.provider.refund("k2", {"amount": 1})
        self.assertEqual(caught.exception.reference, "rate-hold")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(self.ledger.effects_for("k2"), ())

    def test_server_error_after_effect_is_a_server_error(self):
        self.mock.server_error_after_effect = True
        with self.assertRaises(ProviderServerError):
            self.provider.refund("k3", {"amount": 1})
        self.assertEqual(len(self.ledger.effects_for("k3")), 1)

    def test_lookup_answers(self):
        self.provider.refund("k4", {"amount": 1})
        found = self.provider.lookup("k4")
        self.assertEqual((found.status, found.params), ("found", {"amount": 1}))
        self.assertEqual(self.provider.lookup("nope").status, "absent")
        self.mock.lookup_unavailable = True
        with self.assertRaises(LookupUnavailable):
            self.provider.lookup("k4")

    def test_connector_classifies_the_wire_like_the_call(self):
        from connectors.payments import PaymentsConnector

        class Intent:
            connector_id = "mock-payments"
            action_type = "refund_payment"
            provider_idempotency_key = "k5"
            payload_canonical = '{"amount":1}'

        connector = PaymentsConnector(self.provider)
        self.assertIs(connector.dispatch(Intent, operation_id=None).kind,
                      DispatchKind.APPLIED)
        self.mock.reject_next = "card-declined"
        Intent.provider_idempotency_key = "k6"
        result = connector.dispatch(Intent, operation_id=None)
        self.assertEqual((result.kind, result.reference),
                         (DispatchKind.NOT_APPLIED, "card-declined"))


class TransportFailureTests(unittest.TestCase):

    def test_timeouts_and_broken_connections_are_no_response(self):
        for error, expected in (
            (httpx.ReadTimeout("slow"), ProviderTimeout),
            (httpx.ConnectTimeout("slow"), ProviderTimeout),
            (httpx.ConnectError("refused"), ConnectionError),
            (httpx.RemoteProtocolError("reset"), ConnectionError),
        ):
            def handler(request, error=error):
                raise error
            with self.subTest(error=type(error).__name__):
                provider = HttpPaymentsProvider(
                    "http://mock", client=mock_transport(handler))
                with self.assertRaises(expected):
                    provider.refund("k", {"a": 1}, timeout=1)

    def test_off_contract_answers_are_never_decisive(self):
        for response in (
            httpx.Response(200, text="not json"),
            httpx.Response(200, json={"no": "id"}),
            httpx.Response(402, json={"error": {"type": "rejected"}}),
            httpx.Response(418, json={}),
            httpx.Response(302, headers={"location": "http://elsewhere"}),
        ):
            with self.subTest(status=response.status_code):
                provider = HttpPaymentsProvider("http://mock", client=(
                    mock_transport(lambda request, r=response: r)))
                with self.assertRaises(ProviderServerError):
                    provider.refund("k", {"a": 1}, timeout=1)

    def test_malformed_lookups_are_unavailable(self):
        for body in ({"status": "found"}, {"status": "maybe"}, ["x"]):
            with self.subTest(body=body):
                provider = HttpPaymentsProvider("http://mock", client=(
                    mock_transport(lambda request, b=body:
                                   httpx.Response(200, json=b))))
                with self.assertRaises(LookupUnavailable):
                    provider.lookup("k")

    def test_requests_carry_the_key_and_a_timeout(self):
        seen = {}

        def handler(request):
            seen["key"] = request.headers.get("idempotency-key")
            seen["timeout"] = request.extensions["timeout"]
            return httpx.Response(200, json={"id": "re_1"})

        HttpPaymentsProvider("http://mock", client=mock_transport(handler)
                             ).refund("sa1_k", {"a": 1}, timeout=0.75)
        self.assertEqual(seen["key"], "sa1_k")
        self.assertEqual(seen["timeout"]["read"], 0.75)


class CrmAndEmailWireTests(QuietTestClient):

    def setUp(self):
        super().setUp()
        self.ledger = EffectsLedger()
        self.crm = MockCrmProvider(ledger=self.ledger)
        self.email = MockEmailProvider(ledger=self.ledger)
        self.crm_http = HttpCrmProvider("http://mock", client=TestClient(
            crm_app(self.crm, Stall(0)), base_url="http://mock"))
        self.email_http = HttpEmailProvider("http://mock", client=TestClient(
            email_app(self.email, Stall(0)), base_url="http://mock"))

    def test_crm_round_trip(self):
        record = self.crm_http.create_record("op-1", {"sku": "x"})
        self.assertEqual(self.crm_http.search("op-1"), (record,))
        self.crm.reject_next_retryable = "locked"
        with self.assertRaises(CrmValidationRejected) as caught:
            self.crm_http.create_record("op-2", {"sku": "x"})
        self.assertTrue(caught.exception.retryable)
        self.crm.server_error_after_effect = True
        with self.assertRaises(CrmServerError):
            self.crm_http.create_record("op-3", {"sku": "x"})
        self.crm.search_unavailable = True
        with self.assertRaises(CrmSearchUnavailable):
            self.crm_http.search("op-1")

    def test_email_round_trip(self):
        self.assertTrue(self.email_http.send("m1", {"to": "a"}).startswith(
            "msg_"))
        self.email.reject_next = "bad-recipient"
        with self.assertRaises(EmailRejected):
            self.email_http.send("m2", {"to": "b"})
        self.assertEqual(self.ledger.effects_for("m2"), ())


class AdminApiTests(unittest.TestCase):

    def setUp(self):
        self.ledger = EffectsLedger()
        self.mocks = {
            "payments": MockPaymentsProvider(ledger=self.ledger),
            "crm": MockCrmProvider(ledger=self.ledger),
            "email": MockEmailProvider(ledger=self.ledger),
        }
        self.admin = TestClient(admin_app(self.ledger, self.mocks))

    def test_only_listed_fault_switches_can_be_set(self):
        for kind, allowed in FAULTS.items():
            with self.subTest(kind=kind):
                for name in allowed:
                    self.assertTrue(hasattr(self.mocks[kind], name), name)
                self.assertEqual(self.admin.post(
                    f"/faults/{kind}", json={"_ledger": None}).status_code, 422)
        self.assertEqual(self.admin.post(
            "/faults/payments", json={"fail_after_effect": True}).status_code,
            200)
        self.assertTrue(self.mocks["payments"].fail_after_effect)

    def test_ledger_reads(self):
        self.mocks["payments"].refund("k", {"a": 1})
        self.assertEqual(len(self.admin.get(
            "/ledger/effects", params={"correlation": "k"}).json()["effects"]),
            1)
        body = self.admin.get("/ledger").json()
        self.assertEqual(len(body["calls"]), 1)


if __name__ == "__main__":
    unittest.main()
