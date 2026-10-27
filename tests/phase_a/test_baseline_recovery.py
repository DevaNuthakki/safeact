"""B3 recovery counterexamples; independent of SafeAct storage/recovery code."""

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from chaos.baselines.recovery import Attempt, Fact, choose, native_window, retry_delay
from chaos.baselines.store import Request, Tables
from chaos.profiles import profile
from connectors.base import DispatchKind, LookupTarget
from connectors.crm import CrmConnector
from connectors.crm_mock import MockCrmProvider
from connectors.ledger import EffectsLedger
from core.contracts import EvidenceKind, canonical_payload_bytes, canonical_payload_hash


START = datetime(2026,10,23,tzinfo=timezone.utc)
SECOND = timedelta(seconds=1)


def attempt(n=1, fact=Fact.AMBIGUOUS, *, at=START, retryable=False):
    return Attempt(n,at,fact,None if fact is Fact.AMBIGUOUS else f"provider-ref-{n}",retryable)


def decision(connector, attempts, *, at=START+SECOND, **options):
    return choose(profile(connector,"q-v2"),tuple(attempts),at,investigation_started=START,**options)


class BaselineRecoveryTests(unittest.TestCase):
    def test_store_refuses_public_demo_and_unscoped_schema_names(self):
        for schema in ("public","safeact_demo","safeact_baseline_it_",'safeact_baseline_it_";drop schema public;--'):
            with self.assertRaises(ValueError):Tables(schema)

    def test_store_refuses_ambiguous_noncanonical_or_mutable_payload_input(self):
        fields=dict(tenant_id="north",requester_id="requester",request_key="intent",connector_id="mock-crm",
            contract_version="q-v2",provider_account_id="account",action_type="create_replacement_order",resource_id="case")
        for canonical in ('{"sku":"one","sku":"two"}','{ "sku": "two" }','{"amount":1.2}',"[]"):
            with self.assertRaises(ValueError):Request(**fields,payload_canonical=canonical)
        payload=dict(sku="original")
        request=Request.from_payload(**fields,payload=payload)
        payload["sku"]="changed"
        self.assertEqual(request.payload_canonical,'{"sku":"original"}')

    def test_first_attempt_is_distinct_from_a_retry(self):
        for connector in ("mock-payments","mock-crm","mock-email"):
            self.assertEqual(decision(connector,[]).action,"dispatch")

    def test_all_three_capabilities_preserve_uncertainty_without_proof(self):
        self.assertEqual(decision("mock-payments",[attempt()]).action,"lookup")
        self.assertEqual(decision("mock-payments",[attempt()],lookup_checked=True).action,"retry")
        self.assertEqual(decision("mock-crm",[attempt()],lookup_checked=True).action,"wait")
        self.assertEqual(decision("mock-email",[attempt()]).action,"escalate")

    def test_real_empty_lookup_before_late_crm_commit_never_authorizes_another_dispatch(self):
        ledger=EffectsLedger()
        provider=MockCrmProvider(ledger=ledger)
        held=provider.hold_next(respond_now=True)
        self.addCleanup(held.release)
        connector=CrmConnector(provider)
        payload=dict(case_id="case_baseline_late",sku="replacement")
        operation=uuid4()
        intent=SimpleNamespace(connector_id="mock-crm",action_type="create_replacement_order",
                               payload_canonical=canonical_payload_bytes(payload).decode())
        reply=connector.dispatch(intent,operation_id=operation,timeout=.01)
        self.assertEqual(reply.kind,DispatchKind.AMBIGUOUS)
        target=LookupTarget(operation,"account",None,canonical_payload_hash(payload),START,START+SECOND)
        empty=connector.lookup(target,profile("mock-crm","q-v2"))
        self.assertEqual(empty.kind,EvidenceKind.NOT_FOUND)
        self.assertEqual(decision("mock-crm",[attempt()],lookup_checked=True).action,"wait")
        self.assertEqual(ledger.effects_for(str(operation)),())
        held.release()
        self.assertTrue(held.wait_committed(2))
        found=connector.lookup(target,profile("mock-crm","q-v2"))
        self.assertEqual(found.kind,EvidenceKind.APPLIED)
        resolved=Attempt(1,START,Fact.APPLIED,found.reference)
        self.assertEqual(decision("mock-crm",[resolved],at=START+timedelta(seconds=30)).action,"complete")
        self.assertEqual(len(ledger.calls_for(str(operation))),1)
        self.assertEqual(len(ledger.effects_for(str(operation))),1)

    def test_empty_evidence_cannot_be_used_as_an_attempt_proof(self):
        for evidence in (EvidenceKind.NOT_FOUND,EvidenceKind.INCONCLUSIVE):
            with self.assertRaises(ValueError):Attempt(1,START,evidence)

    def test_positive_evidence_wins_over_later_rejection_and_deadlines(self):
        facts=[attempt(fact=Fact.APPLIED),attempt(2,Fact.NOT_APPLIED,retryable=False)]
        self.assertEqual(decision("mock-payments",facts,at=START+timedelta(days=2)).action,"complete")

    def test_a_definitive_rejection_can_retry_without_a_native_key(self):
        for connector in ("mock-payments","mock-crm"):
            p=profile(connector,"q-v2")
            choice=choose(p,(attempt(fact=Fact.NOT_APPLIED,retryable=True),),START+SECOND,investigation_started=START)
            self.assertEqual((choice.action,choice.basis),("retry","all_attempts_not_applied"))
        # The shared email contract permits only one attempt. A proof cannot
        # create an extra attempt budget that the comparison arm was not given.
        self.assertEqual(decision("mock-email",[attempt(fact=Fact.NOT_APPLIED,retryable=True)]).action,"fail")

    def test_one_ambiguous_earlier_attempt_is_not_erased_by_a_later_rejection(self):
        facts=[attempt(),attempt(2,Fact.NOT_APPLIED,retryable=True)]
        self.assertEqual(decision("mock-crm",facts,lookup_checked=True).action,"escalate")
        self.assertEqual(decision("mock-crm",facts,at=START+timedelta(seconds=10)).action,"escalate")

    def test_rejected_or_ambiguous_budget_exhaustion_have_distinct_outcomes(self):
        p=profile("mock-crm","q-v2")
        rejected=tuple(attempt(i,Fact.NOT_APPLIED,retryable=True) for i in range(1,p.max_attempts+1))
        self.assertEqual(decision("mock-crm",rejected).action,"fail")
        mixed=(attempt(),*rejected[1:])
        self.assertEqual(decision("mock-crm",mixed).action,"escalate")

    def test_permanent_rejection_never_dispatches_again(self):
        self.assertEqual(decision("mock-payments",[attempt(fact=Fact.NOT_APPLIED)]).action,"fail")

    def test_review_is_required_even_with_proven_retry_safety(self):
        facts=[attempt(fact=Fact.NOT_APPLIED,retryable=True)]
        self.assertEqual(decision("mock-crm",facts,review_required=True).action,"review")
        self.assertEqual(decision("mock-crm",facts,review_required=True,approval_valid=True).action,"retry")
        self.assertEqual(decision("mock-crm",facts,at=START+timedelta(seconds=8),review_required=True).action,"fail")

    def test_approval_never_makes_a_non_idempotent_ambiguous_retry_safe(self):
        self.assertEqual(decision("mock-crm",[attempt()],lookup_checked=True,
            review_required=True,approval_valid=True).action,"wait")

    def test_backoff_is_checked_after_proof_and_human_approval(self):
        facts=[attempt(fact=Fact.NOT_APPLIED,retryable=True)]
        self.assertEqual(decision("mock-crm",facts,not_before=START+2*SECOND).action,"wait")
        self.assertEqual(decision("mock-crm",facts,at=START+2*SECOND,not_before=START+2*SECOND).action,"retry")

    def test_first_fence_retention_boundary_is_exclusive_and_cannot_be_reset(self):
        p=profile("mock-payments","q-v2-short")
        native=p.contract.native_idempotency
        cutoff=START+native.retention-native.safety_margin
        self.assertTrue(native_window(p,START,cutoff-timedelta(microseconds=1)))
        self.assertFalse(native_window(p,START,cutoff))
        facts=(attempt(at=START),attempt(2,at=cutoff-SECOND))
        choice=choose(p,facts,cutoff,investigation_started=START,lookup_checked=True)
        self.assertEqual(choice.action,"wait")
        self.assertFalse(native_window(p,None,START))

    def test_parameters_and_concurrent_same_key_guarantees_are_required(self):
        p=profile("mock-payments","q-v2")
        for field in ("binds_parameters","protects_concurrent_same_key"):
            contract=replace(p.contract,native_idempotency=replace(p.contract.native_idempotency,**{field:False}))
            self.assertFalse(native_window(replace(p,contract=contract),START,START+SECOND))

    def test_lease_or_horizon_expiry_does_not_turn_ambiguity_into_a_failure(self):
        self.assertEqual(decision("mock-crm",[attempt()],at=START+timedelta(days=1)).action,"escalate")

    def test_incomplete_or_invalid_attempt_history_fails_closed(self):
        for facts in ([attempt(2)],[attempt(),attempt()],[attempt(at=START+2*SECOND)]):
            with self.assertRaises(ValueError):decision("mock-crm",facts)
        with self.assertRaises(ValueError):decision("mock-crm",[attempt()],at=START-SECOND)
        with self.assertRaises(ValueError):Attempt(1,START,Fact.NOT_APPLIED,None,True)
        with self.assertRaises(ValueError):Attempt(1,START,Fact.APPLIED,"actual-reference",True)

    def test_naive_clock_values_are_refused(self):
        with self.assertRaises(ValueError):Attempt(1,START.replace(tzinfo=None),Fact.AMBIGUOUS)
        with self.assertRaises(ValueError):native_window(profile("mock-payments","q-v2"),START,START.replace(tzinfo=None))

    def test_retry_jitter_is_reproducible_capped_and_honors_retry_after(self):
        p=profile("mock-payments","q-v2")
        for ordinal in (1,2,3,10,1000000):
            actual=retry_delay(p,"business-intent",ordinal)
            self.assertEqual(actual,retry_delay(p,"business-intent",ordinal))
            self.assertGreaterEqual(actual,timedelta(0))
            self.assertLessEqual(actual,p.limits.backoff_cap)
            self.assertEqual(retry_delay(p,"business-intent",ordinal,retry_after=5),timedelta(seconds=5))
        for value in (float("nan"),float("inf"),-1,True,"2"):
            with self.assertRaises(ValueError):retry_delay(p,"intent",1,retry_after=value)


if __name__ == "__main__":unittest.main()
