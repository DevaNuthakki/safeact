"""Trace observations through the actual mock wire APIs and HTTP facades."""

import tempfile
import unittest
import warnings

import httpx2 as httpx

from fastapi.testclient import TestClient

from chaos.coverage import observed
from chaos.provider_trace import ObservedProvider
from connectors.crm_mock import MockCrmProvider, CrmServerError
from connectors.http_mock.provider_api import Stall, crm_app, payments_app
from connectors.http_providers import HttpCrmProvider, HttpPaymentsProvider
from connectors.ledger import EffectsLedger
from connectors.payments_mock import MockPaymentsProvider, ProviderRateLimited, ProviderServerError, ProviderTimeout
from deploy.stack.http_trace import HttpTrace, read_trace


class TraceWireTests(unittest.TestCase):
    def setUp(self):
        context = warnings.catch_warnings()
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        warnings.filterwarnings("ignore", message="You should not use the 'timeout' argument")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.writer = HttpTrace(directory.name, "safeact_it_" + "a" * 32)
        self.ledger = EffectsLedger()

    def wire(self, kind):
        raw = MockPaymentsProvider(ledger=self.ledger) if kind == "payments" else MockCrmProvider(ledger=self.ledger)
        proxy = ObservedProvider(kind, raw, self.ledger.record_observation)
        app = payments_app(proxy, Stall(0)) if kind == "payments" else crm_app(proxy, Stall(0))
        client = TestClient(app, base_url="http://mock")
        self.addCleanup(client.close)
        facade = (HttpPaymentsProvider if kind == "payments" else HttpCrmProvider)(
            "http://mock", client=client, observer=self.writer)
        return raw, facade

    def coverage(self):
        return observed(dict(admissions=[], ledger=self.ledger.events(),
                             http=read_trace(self.directory, 0)))

    def test_received_server_error_after_committed_effect(self):
        raw, wire = self.wire("payments")
        raw.server_error_after_effect = True
        with self.assertRaises(ProviderServerError):
            wire.refund("key", {"charge_id": "ch_test", "amount_minor": 100})
        self.assertEqual(len(self.ledger.effects_for("key")), 1)
        self.assertIn("server_error_after_commit", self.coverage())
        self.assertNotIn("effect_applied_response_lost", self.coverage())

    def test_actual_429_then_same_key_request(self):
        raw, wire = self.wire("payments")
        raw.rate_limit_next = 1
        with self.assertRaises(ProviderRateLimited):
            wire.refund("key", {"amount": 100})
        wire.refund("key", {"amount": 100})
        self.assertEqual(len(self.ledger.effects_for("key")), 1)
        self.assertIn("rate_limited_then_retried", self.coverage())

    def test_empty_search_before_held_commit(self):
        raw, wire = self.wire("crm")
        held = raw.hold_next(respond_now=True)
        self.addCleanup(held.release)
        with self.assertRaises(CrmServerError):
            wire.create_record("key", {"case_id": "case_test", "sku": "sku"})
        self.assertEqual(wire.search("key"), ())
        held.release()
        self.assertTrue(held.wait_committed())
        self.assertIn("late_commit_after_empty_lookup", self.coverage())

    def test_corrupt_lookup_is_compared_with_independent_effect_and_payload(self):
        raw, wire = self.wire("crm")
        wire.create_record("key", {"case_id": "case_test", "sku": "sku"})
        raw.corrupt_search = True
        wire.search("key")
        self.assertIn("lookup_lied_outside_contract", self.coverage())

    def test_http_transport_timeout_keeps_the_applied_effect(self):
        raw = MockPaymentsProvider(ledger=self.ledger)
        proxy = ObservedProvider("payments", raw, self.ledger.record_observation)
        def handler(request):
            proxy.refund("key", {"amount": 100})
            raise httpx.ReadTimeout("simulated lost reply")
        with httpx.Client(base_url="http://mock", transport=httpx.MockTransport(handler)) as client:
            wire = HttpPaymentsProvider("http://mock", client=client, observer=self.writer)
            with self.assertRaises(ProviderTimeout):
                wire.refund("key", {"amount": 100}, timeout=1)
        self.assertIn("effect_applied_response_lost", self.coverage())


if __name__ == "__main__":
    unittest.main()
