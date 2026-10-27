"""Actual socket timeout after a provider applied an effect; mocks only."""

import os
import tempfile
import unittest

import httpx2 as httpx

from chaos.coverage import observed
from chaos.provider_trace import ObservedProvider
from connectors.crm_mock import MockCrmProvider
from connectors.email_mock import MockEmailProvider
from connectors.http_mock.servers import MockProviderServers
from connectors.http_providers import HttpPaymentsProvider
from connectors.ledger import EffectsLedger
from connectors.payments_mock import MockPaymentsProvider, ProviderTimeout
from deploy.stack.http_trace import HttpTrace, read_trace


@unittest.skipUnless(os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1", "explicit integration opt-in")
class TraceSocketTests(unittest.TestCase):
    def test_eventual_payment_empty_lookup_returns_over_http_before_the_held_effect(self):
        import threading
        from chaos.recovery_faults import ShortRetentionPayments, release_after_empty_lookup
        from chaos.runner import _ledger_for
        from types import SimpleNamespace

        ledger = EffectsLedger()
        payment = ShortRetentionPayments(ledger=ledger)
        payment.register_key("eventual", "q-v2-eventual")
        payment.hide_new_effects = True
        providers = (payment, MockCrmProvider(ledger=ledger), MockEmailProvider(ledger=ledger))
        proxies = tuple(ObservedProvider(kind, provider, ledger.record_observation)
                        for kind, provider in zip(("payments", "crm", "email"), providers))
        servers = MockProviderServers(ledger=ledger, providers=proxies, stall_seconds=3)
        held = None
        try:
            servers.start()
            with tempfile.TemporaryDirectory() as directory, httpx.Client(
                    base_url=servers.url("payments"), trust_env=False) as client:
                writer = HttpTrace(directory, "safeact_it_" + "b" * 32)
                wire = HttpPaymentsProvider(servers.url("payments"), client=client, observer=writer)
                wire.refund("warmup", {"charge_id":"ch_warmup","amount_minor":1}, timeout=2)
                held = payment.hold_next(respond_now=True)
                with self.assertRaises(ProviderTimeout):
                    wire.refund("eventual", {"charge_id":"ch_target","amount_minor":100}, timeout=.3)
                self.assertEqual(wire.lookup("eventual").status, "absent")
                self.assertEqual(ledger.effects_for("eventual"), ())
                def observe():
                    return dict(ledger=_ledger_for(SimpleNamespace(ledger=ledger), {"ch_target"}, 0),
                                http=read_trace(directory, 0))
                self.assertTrue(release_after_empty_lookup(held, threading.Event(), observe,
                    lambda *a, **k: None, provider="payments", resources={"ch_target"}, timeout=2))
                self.assertEqual(len(ledger.effects_for("eventual")), 1)
                self.assertEqual(wire.lookup("eventual").status, "absent")
                payment.reveal_hidden_effects()
                self.assertEqual(wire.lookup("eventual").status, "found")
                self.assertIn("late_commit_after_empty_lookup", observed(observe()))
        finally:
            if held is not None:
                held.release()
                if held.started.is_set():
                    self.assertTrue(held.wait_committed(2))
            servers.stop()

    def test_lost_response_after_effect_uses_real_socket_timeout(self):
        ledger = EffectsLedger()
        payment = MockPaymentsProvider(ledger=ledger)
        providers = (payment, MockCrmProvider(ledger=ledger), MockEmailProvider(ledger=ledger))
        proxies = tuple(ObservedProvider(kind, provider, ledger.record_observation)
                        for kind, provider in zip(("payments", "crm", "email"), providers))
        servers = MockProviderServers(ledger=ledger, providers=proxies, stall_seconds=3)
        try:
            servers.start()
            with tempfile.TemporaryDirectory() as directory, httpx.Client(
                    base_url=servers.url("payments"), trust_env=False) as client:
                writer = HttpTrace(directory, "safeact_it_" + "a" * 32)
                wire = HttpPaymentsProvider(servers.url("payments"), client=client, observer=writer)
                wire.refund("warmup", {"amount": 10}, timeout=2)
                payment.fail_after_effect = True
                with self.assertRaises(ProviderTimeout):
                    wire.refund("lost", {"amount": 100}, timeout=1)
                self.assertEqual(len(ledger.effects_for("lost")), 1)
                self.assertEqual(len(ledger.calls_for("lost")), 1)
                found = observed(dict(admissions=[], ledger=ledger.events(),
                                      http=read_trace(directory, 0)))
                self.assertIn("effect_applied_response_lost", found)
        finally:
            servers.stop()


if __name__ == "__main__":
    unittest.main()
