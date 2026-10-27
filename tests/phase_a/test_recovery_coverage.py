"""Counterexamples for observed deadline, cache expiry and late-commit coverage."""

import copy
import json
import threading
import unittest

from types import SimpleNamespace
from unittest.mock import patch

from chaos import coverage, qualify
from chaos.database_trace import recovery_rows
from chaos.recovery_faults import ObservedOutage, ShortRetentionPayments, release_after_escalation
from chaos.runner import FaultDriver, Timeline
from chaos.scenarios import Scenario, generate
from connectors.http_trace import correlation_hash
from connectors.ledger import EffectsLedger
from connectors.payments_mock import MockPaymentsProvider, ProviderTimeout


def evidence(kind):
    digest = correlation_hash("private-correlation")
    def event(name, at, **details):
        return dict(source="http_client" if name.startswith("http_") else "provider",
                    event=name, monotonic_ns=at, provider="payments", correlation_sha256=digest, **details)
    def sample(at, *, state="UNKNOWN", passed=False):
        return recovery_rows([("op", 4, state, "mock-payments", "private-correlation",
                               99999999, passed, 99999998 if state == "ESCALATED_UNRESOLVED" else None)], at, at+1)[0]
    raw = dict(admissions=[dict(index=0, local_id="op", changed=False)],
               observation_finished_monotonic_ns=200, ledger=[], http=[], database=[])
    if kind == "outage":
        raw["ledger"] = [event("provider_outage_changed", 10, outage_id="outage", active=True),
            event("provider_start", 30, method="refund", provider_call_id="provider"),
            event("provider_error", 35, method="refund", provider_call_id="provider", error_type="ProviderTimeout"),
            event("provider_outage_changed", 100, outage_id="outage", active=False)]
        raw["http"] = [event("http_start", 20, request_id="http", method="POST"),
                       event("http_error", 40, request_id="http", method="POST", error_type="ReadTimeout")]
        raw["database"] = [sample(45), sample(65, passed=True)]
    else:
        raw["ledger"] = [event("provider_start", 20, method="refund", provider_call_id="provider"),
            event("call", 21),
            event("provider_error", 30, method="refund", provider_call_id="provider", error_type="ProviderTimeout")]
        raw["http"] = [event("http_start", 10, request_id="http", method="POST"),
                       event("http_error", 40, request_id="http", method="POST", error_type="ReadTimeout")]
        if kind == "expiry":
            raw["ledger"] += [event("effect", 23, effect_id="effect"),
                event("provider_key_stored", 24, effect_id="effect", retention_seconds=6),
                event("provider_key_expired", 70, effect_id="effect", retention_seconds=6, age_seconds=6.1,
                      key_present_before=True, key_present_after=False)]
            raw["database"] = [sample(50), sample(80)]
        else:
            raw["ledger"] += [event("effect", 90, effect_id="effect")]
            raw["database"] = [sample(50, state="ESCALATED_UNRESOLVED", passed=True)]
    raw["ledger"].sort(key=lambda e:e["monotonic_ns"])
    return raw


LABELS = dict(outage="provider_outage_spanned_deadline", expiry="retention_expired_before_outcome",
              late="late_commit_after_escalation")


class RecoveryCoverageTests(unittest.TestCase):
    def test_independent_sources_prove_all_three_interleavings(self):
        for kind, label in LABELS.items():
            self.assertIn(label, coverage.observed(evidence(kind)))

    def test_arm_views_and_schedules_cannot_supply_missing_database_proof(self):
        for kind, label in LABELS.items():
            raw=evidence(kind);raw["database"]=[]
            raw["operations"]={"op":dict(state="ESCALATED_UNRESOLVED", escalated_at=1)}
            raw["timeline"]=[dict(event="outage_on"),dict(event="key_expired"),dict(event="hold_released")]
            self.assertNotIn(label, coverage.observed(raw))

    def test_unrelated_or_unadmitted_database_samples_cannot_supply_credit(self):
        for kind, label in LABELS.items():
            for field, value in (("operation_id","other"),("provider","crm"),("correlation_sha256","other"),("source","arm")):
                raw=evidence(kind)
                for row in raw["database"]:row[field]=value
                self.assertNotIn(label,coverage.observed(raw),(kind,field))

    def test_invalid_query_intervals_and_cleanup_only_samples_are_rejected(self):
        for kind, label in LABELS.items():
            raw=evidence(kind);raw["database"][-1]["sample_started_ns"]=999
            self.assertNotIn(label,coverage.observed(raw))
            raw=evidence(kind);raw["observation_finished_monotonic_ns"]=44
            self.assertNotIn(label,coverage.observed(raw))

    def test_outage_must_bracket_a_crossing_of_the_same_database_deadline(self):
        for changes in (dict(deadline_passed=False),dict(investigation_deadline=2),
                        dict(sample_started_ns=40,sample_finished_ns=41,monotonic_ns=41)):
            raw=evidence("outage");raw["database"][-1].update(changes)
            self.assertNotIn(LABELS["outage"],coverage.observed(raw))

    def test_missing_recovery_or_interrupted_outage_window_is_not_continuous(self):
        raw=evidence("outage");raw["ledger"].pop()
        self.assertNotIn(LABELS["outage"],coverage.observed(raw))
        raw=evidence("outage");raw["ledger"].append(dict(raw["ledger"][-1],outage_id="other",monotonic_ns=55))
        self.assertNotIn(LABELS["outage"],coverage.observed(raw))

    def test_outage_switch_without_actual_failed_provider_and_http_requests_is_insufficient(self):
        for key in ("http","ledger"):
            raw=evidence("outage")
            if key=="http":raw[key]=[]
            else:raw[key]=[e for e in raw[key] if e["event"]=="provider_outage_changed"]
            self.assertNotIn(LABELS["outage"],coverage.observed(raw))

    def test_different_error_or_successful_wire_answer_does_not_prove_an_outage_failure(self):
        raw=evidence("outage");raw["ledger"][2]["error_type"]="ValueError"
        self.assertNotIn(LABELS["outage"],coverage.observed(raw))
        raw=evidence("outage");raw["http"][-1].update(event="http_response",status_code=200)
        self.assertNotIn(LABELS["outage"],coverage.observed(raw))

    def test_never_stored_keys_and_empty_lookups_do_not_prove_expiry(self):
        for event in ("provider_key_stored","effect"):
            raw=evidence("expiry");raw["ledger"]=[e for e in raw["ledger"] if e["event"]!=event]
            self.assertNotIn(LABELS["expiry"],coverage.observed(raw))

    def test_actual_removal_age_and_matching_stored_effect_are_required(self):
        for changes in (dict(key_present_after=True),dict(key_present_before=False),dict(age_seconds=5.9),
                        dict(retention_seconds=float("nan")),dict(effect_id="other")):
            raw=evidence("expiry");raw["ledger"][-1].update(changes)
            self.assertNotIn(LABELS["expiry"],coverage.observed(raw))

    def test_already_resolved_state_or_received_success_does_not_prove_unknown_at_expiry(self):
        raw=evidence("expiry");raw["database"][-1]["state"]="SUCCEEDED"
        self.assertNotIn(LABELS["expiry"],coverage.observed(raw))
        raw=evidence("expiry");raw["http"][-1].update(event="http_response",status_code=200)
        self.assertNotIn(LABELS["expiry"],coverage.observed(raw))

    def test_post_escalation_visibility_of_an_earlier_effect_is_not_a_late_commit(self):
        raw=evidence("late");raw["ledger"].append(dict(raw["ledger"][-1],monotonic_ns=25))
        self.assertNotIn(LABELS["late"],coverage.observed(raw))

    def test_a_new_dispatch_after_escalation_cannot_supply_the_late_effect(self):
        raw=evidence("late");raw["ledger"].append(dict(raw["ledger"][1],monotonic_ns=80))
        self.assertNotIn(LABELS["late"],coverage.observed(raw))

    def test_escalation_requires_a_committed_query_after_the_original_lost_request(self):
        raw=evidence("late");raw["database"][0]["escalated_at"]=None
        self.assertNotIn(LABELS["late"],coverage.observed(raw))
        raw=evidence("late");raw["http"][-1]["monotonic_ns"]=60
        self.assertNotIn(LABELS["late"],coverage.observed(raw))

    def test_fault_coverage_is_independent_of_claimed_safety_and_completion(self):
        for kind,label in LABELS.items():
            raw=evidence(kind);raw.update(integrity=["unsafe"],harness_errors=["error"])
            raw["operations"]={"op":dict(state="FAILED_CONFIRMED")}
            self.assertIn(label,coverage.observed(raw))


class RecoveryMechanismTests(unittest.TestCase):
    def test_provider_outage_observations_are_synchronized_and_requests_really_fail(self):
        ledger=EffectsLedger();provider=MockPaymentsProvider(ledger=ledger)
        def observe(event,**details):
            self.assertTrue(provider._lock.locked());ledger.record_observation(event,**details)
        outage=ObservedOutage("payments",provider,observe)
        outage.set(True)
        with self.assertRaises(ProviderTimeout):provider.refund("key",{})
        outage.set(False);provider.refund("key",{})
        rows=[e for e in ledger.events() if e["event"]=="provider_outage_changed"]
        self.assertEqual([r["active"] for r in rows],[True,False])
        self.assertEqual(rows[0]["outage_id"],rows[1]["outage_id"])
        self.assertEqual(len(ledger.effects_for("key")),1)
        with self.assertRaises(RuntimeError):outage.set(False)

    def test_expiry_removes_only_real_expired_entries_and_does_not_read_the_ledger(self):
        ledger=EffectsLedger();provider=ShortRetentionPayments(ledger=ledger)
        clock=[100.0];provider._clock=lambda:clock[0];provider.short_keys.add("short")
        provider.refund("short",{});provider.refund("long",{})
        clock[0]=105.99
        with provider._lock:provider._expire()
        self.assertIn("short",provider._by_key)
        clock[0]=106.01
        with patch.object(ledger,"events",side_effect=AssertionError("ledger read")),patch.object(ledger,"snapshot",side_effect=AssertionError("ledger read")):
            with provider._lock:provider._expire()
        self.assertNotIn("short",provider._by_key);self.assertIn("long",provider._by_key)
        expired=[e for e in ledger.events() if e["event"]=="provider_key_expired"]
        self.assertEqual(len(expired),1);self.assertGreaterEqual(expired[0]["age_seconds"],6)
        self.assertNotIn('"short"',json.dumps(expired))

    def test_repeated_expiry_sampling_cannot_manufacture_a_removed_key(self):
        ledger=EffectsLedger();provider=ShortRetentionPayments(ledger=ledger)
        provider.short_keys.add("missing")
        with provider._lock:
            provider._expire();provider._expire()
        self.assertEqual(ledger.events(),())

    def test_recovery_trace_keeps_database_comparison_without_host_wall_clock_math(self):
        row=recovery_rows([("op",2,"UNKNOWN","mock-payments","secret-key",1e20,True,None)],10,20)[0]
        self.assertTrue(row["deadline_passed"]);self.assertEqual(row["investigation_deadline"],1e20)
        self.assertNotIn("secret-key",json.dumps(row))
        for broken in (("op",2,"UNKNOWN","mock-payments","key",None,True,None),
                       ("op",2,"ESCALATED_UNRESOLVED","mock-payments","key",1,True,None)):
            with self.assertRaises(ValueError):recovery_rows([broken],10,20)

    def test_late_hold_waits_for_independent_escalation_before_releasing(self):
        from connectors.mock_support import HeldCall
        held=HeldCall(respond_now=True);held.started.set();cancelled=threading.Event()
        rows=iter([[],[dict(state="UNKNOWN")],evidence("late")["database"]])
        def release():held.committed.set()
        with patch.object(held,"release",side_effect=release) as released:
            self.assertTrue(release_after_escalation(held,cancelled,lambda:next(rows),lambda *a,**k:None,timeout=1))
            released.assert_called_once()

    def test_cancelled_hold_is_left_for_the_ordered_cleanup_barrier(self):
        from connectors.mock_support import HeldCall
        held=HeldCall();cancelled=threading.Event();cancelled.set()
        with patch.object(held,"release") as released:
            self.assertFalse(release_after_escalation(held,cancelled,lambda:[],lambda *a,**k:None))
            released.assert_not_called()

    def test_unobserved_or_ambiguous_escalation_cannot_complete_the_fault(self):
        from connectors.mock_support import HeldCall
        held=HeldCall();held.started.set()
        with self.assertRaises(RuntimeError):
            release_after_escalation(held,threading.Event(),lambda:[],lambda *a,**k:None,timeout=0)
        with self.assertRaises(RuntimeError):
            release_after_escalation(held,threading.Event(),lambda:[{},{}],lambda *a,**k:None,timeout=1)

    def test_zero_offset_outage_is_active_synchronously_before_admission(self):
        active=[];provider=SimpleNamespace()
        slot=SimpleNamespace(mocks={"payments":provider},proxies={"payments":SimpleNamespace()},set_outage=lambda k,v:active.append(v))
        driver=FaultDriver(slot,Timeline())
        driver.apply(dict(kind="outage",provider="payments",at_ms=0,duration_ms=60000))
        self.assertEqual(active,[True]);driver.cancel();self.assertFalse(driver.pending())

    def test_late_hold_fault_requires_observed_completion(self):
        from connectors.mock_support import HeldCall
        mock=SimpleNamespace(hold_next=lambda **k:HeldCall(**k))
        arm=SimpleNamespace(release_late_commit=lambda *a:False)
        driver=FaultDriver(SimpleNamespace(mocks={"payments":mock},proxies={"payments":None}),Timeline(),arm=arm)
        driver.apply(dict(kind="hold_after_escalation",provider="payments"));driver.cancel()
        self.assertTrue(any("hold_after_escalation" in error for error in driver.errors))

    def test_deadline_variants_round_trip_without_promising_completion_outside_the_window(self):
        for family in ("F07","F16"):
            cases=[generate("dev",family,i,7) for i in (3,4)]
            self.assertEqual([s.connector for s in cases],["payments","crm"])
            for case in cases:self.assertEqual(case,Scenario.from_json(case.to_json()))
        self.assertFalse(generate("dev","F16",3,7).completion_achievable)
        self.assertFalse(generate("dev","F16",4,7).completion_achievable)

    def test_case_selection_rejects_unknown_duplicates_empty_and_held_out(self):
        args=SimpleNamespace(corpus="dev",families="F16",limit=None,cases="D-F16-0003,D-F16-0004")
        self.assertEqual([s.scenario_id for s in qualify.scenarios_for(args)],["D-F16-0003","D-F16-0004"])
        for cases in ("", "D-F16-0003,", "D-F16-0003,D-F16-0003", "D-F07-0000", "Q-F16-0003"):
            args.cases=cases
            with self.assertRaises(SystemExit):qualify.scenarios_for(args)


if __name__ == "__main__":
    unittest.main()
