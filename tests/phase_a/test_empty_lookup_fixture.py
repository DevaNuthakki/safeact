"""Counterexamples for contract wiring and the observed empty-lookup barrier."""

import copy
import threading
import unittest

from types import SimpleNamespace
from unittest.mock import patch

from chaos.coverage import observed
from chaos.lookup_trace import empty_lookup_windows
from chaos.provider_trace import ObservedProvider
from chaos.recovery_faults import ShortRetentionPayments, release_after_empty_lookup
from chaos.runner import FaultDriver, Timeline
from chaos.scenarios import generate
from connectors.http_trace import correlation_hash
from connectors.ledger import EffectsLedger
from connectors.mock_support import HeldCall
from connectors.payments_mock import IdempotencyConflict, LookupUnavailable, ProviderInFlight, ProviderTimeout


LABEL = "late_commit_after_empty_lookup"


def lookup_raw():
    key = correlation_hash("private-key")
    def event(name, ns, **details):
        return dict(event=name, monotonic_ns=ns, provider="payments", correlation_sha256=key,
                    source="http_client" if name.startswith("http_") else "provider", **details)
    return dict(admissions=[], observation_finished_monotonic_ns=100,
        ledger=[event("call", 5, resource="ch_target"),
            event("provider_start", 20, method="lookup", provider_call_id="lookup"),
            event("provider_end", 30, method="lookup", provider_call_id="lookup",
                  lookup_status="absent", lookup_records=[])],
        http=[event("http_start", 10, method="GET", path="/v1/lookup", request_id="get"),
              event("http_response", 40, method="GET", path="/v1/lookup", request_id="get", status_code=200)])


class EmptyLookupFixtureTests(unittest.TestCase):
    def test_eventual_keys_have_empty_pending_lookup_and_hidden_commit_until_reveal(self):
        ledger=EffectsLedger();provider=ShortRetentionPayments(ledger=ledger)
        provider.register_key("eventual","q-v2-eventual");provider.hide_new_effects=True
        held=provider.hold_next(respond_now=True);self.addCleanup(held.release)
        wire=ObservedProvider("payments",provider,ledger.record_observation)
        with self.assertRaises(ProviderTimeout):wire.refund("eventual",{"amount":10})
        self.assertTrue(held.started.is_set());self.assertEqual(wire.lookup("eventual").status,"absent")
        self.assertEqual(ledger.effects_for("eventual"),())
        held.release();self.assertTrue(held.wait_committed())
        self.assertEqual(wire.lookup("eventual").status,"absent")
        provider.reveal_hidden_effects()
        self.assertEqual(wire.lookup("eventual").status,"found")
        self.assertEqual(len(ledger.effects_for("eventual")),1)

    def test_strong_keys_still_expose_in_progress_then_the_committed_effect(self):
        ledger=EffectsLedger();provider=ShortRetentionPayments(ledger=ledger)
        provider.register_key("strong","q-v2");provider.hide_new_effects=True
        held=provider.hold_next(respond_now=True);self.addCleanup(held.release)
        with self.assertRaises(ProviderTimeout):provider.refund("strong",{})
        self.assertEqual(provider.lookup("strong").status,"in_progress")
        held.release();self.assertTrue(held.wait_committed())
        self.assertEqual(provider.lookup("strong").status,"found")

    def test_eventual_lookup_preserves_native_key_concurrency_replay_and_parameter_binding(self):
        ledger=EffectsLedger();provider=ShortRetentionPayments(ledger=ledger)
        provider.register_key("key","q-v2-eventual")
        held=provider.hold_next(respond_now=True);self.addCleanup(held.release)
        with self.assertRaises(ProviderTimeout):provider.refund("key",{"amount":10})
        with self.assertRaises(ProviderInFlight):provider.refund("key",{"amount":10})
        held.release();self.assertTrue(held.wait_committed())
        receipt=provider.refund("key",{"amount":10});self.assertTrue(receipt.replayed)
        with self.assertRaises(IdempotencyConflict):provider.refund("key",{"amount":11})
        self.assertEqual(len(ledger.effects_for("key")),1)

    def test_key_registration_and_unavailable_outage_controls_are_not_bypassed(self):
        provider=ShortRetentionPayments(ledger=EffectsLedger())
        for key,version in (("","q-v2"),(None,"q-v2"),("key","other")):
            with self.assertRaises(ValueError):provider.register_key(key,version)
        provider.register_key("key","q-v2-eventual")
        for flag in ("lookup_unavailable","outage"):
            setattr(provider,flag,True)
            with self.assertRaises(LookupUnavailable):provider.lookup("key")
            setattr(provider,flag,False)

    def test_completed_empty_http_lookup_is_joined_for_both_connectors(self):
        for kind,method,path in (("payments","lookup","/v1/lookup"),("crm","search","/records")):
            raw=lookup_raw()
            for row in raw["ledger"]:row["provider"]=kind
            for row in raw["ledger"][1:]:row["method"]=method
            for row in raw["http"]:row.update(provider=kind,path=path)
            self.assertEqual(len(empty_lookup_windows(raw)),1)
            raw["ledger"].append(dict(raw["ledger"][0],event="effect",monotonic_ns=50))
            self.assertIn(LABEL,observed(raw))

    def test_in_progress_nonempty_failed_or_missing_http_results_receive_no_credit(self):
        for changes in (dict(lookup_status="in_progress"),dict(lookup_records=[{"effect_id":"one"}])):
            raw=lookup_raw();raw["ledger"][-1].update(changes)
            self.assertEqual(empty_lookup_windows(raw),[])
        for change in ("missing","error","status"):
            raw=lookup_raw()
            if change=="missing":raw["http"].pop()
            elif change=="error":raw["http"][-1]["event"]="http_error"
            else:raw["http"][-1]["status_code"]=503
            self.assertEqual(empty_lookup_windows(raw),[])

    def test_wrong_binding_path_or_ambiguous_and_unfinished_requests_are_not_joined(self):
        for field,value in (("provider","crm"),("correlation_sha256","other"),("path","/ledger")):
            raw=lookup_raw()
            for row in raw["http"]:row[field]=value
            self.assertEqual(empty_lookup_windows(raw),[])
        for unfinished in (False,True):
            raw=lookup_raw()
            raw["http"] += [dict(e,request_id="other") for e in copy.deepcopy(raw["http"][:1 if unfinished else 2])]
            self.assertEqual(empty_lookup_windows(raw),[])
        raw=lookup_raw();raw["ledger"].append(dict(raw["ledger"][1],provider_call_id="unfinished",monotonic_ns=25))
        self.assertEqual(empty_lookup_windows(raw),[])

    def test_effect_before_http_return_and_cleanup_only_return_are_not_late_coverage(self):
        raw=lookup_raw();raw["ledger"].append(dict(raw["ledger"][0],event="effect",monotonic_ns=35))
        self.assertNotIn(LABEL,observed(raw))
        raw["ledger"][-1]["monotonic_ns"]=50;raw["observation_finished_monotonic_ns"]=35
        self.assertNotIn(LABEL,observed(raw))

    def test_hold_is_released_only_after_the_caller_received_the_empty_result(self):
        held=HeldCall(respond_now=True);held.started.set();raw=lookup_raw();seen=[]
        first=copy.deepcopy(raw);first["http"].pop();samples=iter([first,raw])
        def observe():
            self.assertFalse(held._release.is_set());return next(samples)
        def release():held.committed.set()
        with patch.object(held,"release",side_effect=release) as released:
            self.assertTrue(release_after_empty_lookup(held,threading.Event(),observe,
                lambda event,**kw:seen.append(event),provider="payments",resources={"ch_target"},timeout=1))
            released.assert_called_once()
        self.assertEqual(seen,["empty_lookup_observed","hold_released"])

    def test_missing_or_unrelated_lookup_times_out_without_releasing_the_hold(self):
        for change in ("missing","unrelated","already_applied"):
            held=HeldCall();held.started.set();raw=lookup_raw()
            if change=="missing":raw["http"].pop()
            elif change=="unrelated":raw["ledger"][0]["resource"]="other"
            else:raw["ledger"].append(dict(raw["ledger"][0],event="effect",monotonic_ns=45))
            with patch.object(held,"release") as released,patch("chaos.recovery_faults.time.monotonic",side_effect=[0,.01,.02,.3]):
                with self.assertRaises(RuntimeError):
                    release_after_empty_lookup(held,threading.Event(),lambda:raw,lambda *a,**k:None,
                        provider="payments",resources={"ch_target"},timeout=.2)
                released.assert_not_called()

    def test_cancellation_leaves_unconsumed_and_consumed_holds_to_cleanup(self):
        for consumed in (False,True):
            held=HeldCall();cancelled=threading.Event();cancelled.set()
            if consumed:held.started.set()
            with patch.object(held,"release") as released:
                self.assertFalse(release_after_empty_lookup(held,cancelled,lambda:lookup_raw(),
                    lambda *a,**k:None,provider="payments",resources={"ch_target"}))
                released.assert_not_called()

    def test_observed_empty_lookup_does_not_shorten_the_seeded_minimum_delay(self):
        held=HeldCall();held.started.set();clock=[0.0];samples=[];released_at=[]
        def now():clock[0]+=.05;return clock[0]
        def observe():samples.append(clock[0]);return lookup_raw()
        def release():released_at.append(clock[0]);held.committed.set()
        with patch("chaos.recovery_faults.time.monotonic",side_effect=now),patch.object(held,"release",side_effect=release):
            self.assertTrue(release_after_empty_lookup(held,threading.Event(),observe,lambda *a,**k:None,
                provider="payments",resources={"ch_target"},minimum_seconds=.25,timeout=2))
        self.assertGreater(len(samples),1);self.assertGreaterEqual(released_at[0],.4)

    def test_driver_propagates_trace_failure_and_requires_fault_completion(self):
        held=HeldCall();held.started.set()
        arm=SimpleNamespace(trace_observations=lambda _:(_ for _ in ()).throw(RuntimeError("trace unavailable")))
        slot=SimpleNamespace(mocks={"payments":SimpleNamespace(hold_next=lambda **kw:held)},proxies={},ledger=EffectsLedger())
        driver=FaultDriver(slot,Timeline(),{"ch_target"},arm=arm)
        driver.apply(dict(kind="hold_after_empty_lookup",provider="payments",release_ms=0))
        driver.timers[0].join(1);driver.cancel()
        self.assertTrue(any("trace unavailable" in e for e in driver.errors))
        self.assertTrue(any("hold_after_empty_lookup" in e for e in driver.errors))
        self.assertFalse(held._release.is_set())

    def test_seeded_f07_delays_and_post_escalation_cases_remain_explicit(self):
        for index,delay in enumerate((3562,2187,2188)):
            case=generate("dev","F07",index,7)
            self.assertEqual(case.faults[0]["kind"],"hold_after_empty_lookup")
            self.assertEqual(case.faults[0]["release_ms"],delay)
            self.assertEqual(case.contract_version,"q-v2-eventual" if case.connector=="payments" else "q-v2")
        for index in (3,4):
            self.assertEqual(generate("dev","F07",index,7).faults[0]["kind"],"hold_after_escalation")


if __name__ == "__main__":
    unittest.main()
