"""Response-completion binding of native-key negative lookup evidence."""

import unittest
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from uuid import uuid4

from chaos.profiles import profile
from connectors.base import LookupResult,LookupTarget,completed_lookup
from core.contracts import EvidenceKind


class LookupCompletionUnitTests(unittest.TestCase):
    def setUp(self):
        self.start=datetime(2026,10,23,tzinfo=timezone.utc)
        self.profile=profile("mock-payments","q-v2-short")
        self.target=LookupTarget(uuid4(),"account","key","hash",self.start,self.start+timedelta(seconds=3))
        self.negative=LookupResult(EvidenceKind.PROVEN_NOT_APPLIED,"actual-lookup-reference",
                                   provider_facts={"provider_note":"retained"})

    def test_completion_before_cutoff_keeps_real_nonapplication_proof(self):
        self.assertIs(completed_lookup(self.profile,self.target,self.negative,self.start+timedelta(seconds=3.9)),self.negative)

    def test_cutoff_is_exclusive_and_late_answer_never_keeps_reference(self):
        for seconds in (4,6,100):
            result=completed_lookup(self.profile,self.target,self.negative,self.start+timedelta(seconds=seconds))
            self.assertEqual(result.kind,EvidenceKind.NOT_FOUND)
            self.assertIsNone(result.reference)
            self.assertEqual(result.provider_facts,self.negative.provider_facts)

    def test_missing_finish_or_first_dispatch_is_not_decisive(self):
        for target,finish in ((self.target,None),(replace(self.target,first_dispatch_at=None),self.start+timedelta(seconds=3.5))):
            self.assertEqual(completed_lookup(self.profile,target,self.negative,finish).kind,EvidenceKind.NOT_FOUND)

    def test_reversed_database_time_is_inconclusive(self):
        result=completed_lookup(self.profile,self.target,self.negative,self.start+timedelta(seconds=2))
        self.assertEqual(result.kind,EvidenceKind.INCONCLUSIVE)
        self.assertIsNone(result.reference)

    def test_matching_positive_receipt_stays_decisive_after_retention(self):
        positive=LookupResult(EvidenceKind.APPLIED,"actual-effect-reference")
        self.assertIs(completed_lookup(self.profile,self.target,positive,self.start+timedelta(days=1)),positive)

    def test_other_nondecisive_answers_remain_unchanged(self):
        for kind in (EvidenceKind.NOT_FOUND,EvidenceKind.INCONCLUSIVE):
            answer=LookupResult(kind,note="provider_unavailable")
            self.assertIs(completed_lookup(self.profile,self.target,answer,None),answer)

    def test_non_native_authoritative_lookup_is_not_given_a_fictitious_key_window(self):
        crm=profile("mock-crm","q-v2")
        self.assertIs(completed_lookup(crm,self.target,self.negative,self.start+timedelta(days=1)),self.negative)


if __name__=="__main__":unittest.main()
