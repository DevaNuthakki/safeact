"""Email cells in F05, F06 and F16 (STOP 2 review): A3 for an irreversible
connector without lookup, added without changing any other scenario."""

import hashlib
import json
import unittest
from collections import Counter
from dataclasses import asdict

from chaos import frozen
from chaos.qualify import DEV_COUNTS, DEV_SEED
from chaos.scenarios import EMAIL_SLOTS, _email_slot, corpus, digest


# Before the email cells: the 75 reviewed development cases, and the
# qualification corpus without its 39 new email slots (from 30d7bd8).
DEV_BEFORE_SHA256 = "a144bfe75c020b647004bc10cf4e3ea4a65e57f6640982464266a540db870bef"
QUAL_KEPT_SHA256 = "a9150370871c9c834dae3f9c09bb7997fccb0148ae4c0da330d5ebcc025f15ef"
DEV_BEFORE_COUNTS = dict(DEV_COUNTS, F05=5, F06=4, F16=5)


def index(case):
    return int(case.scenario_id[-4:])


def content(case):
    data = asdict(case)
    data.pop("scenario_id"), data.pop("split"), data.pop("seed")
    return json.dumps(data, sort_keys=True)


class EmailCellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qual = frozen.scenarios()
        cls.dev = corpus("dev", DEV_COUNTS, DEV_SEED)

    def email(self, family, cases=None):
        return [c for c in (cases or self.qual) if c.family == family and c.connector == "email"]

    def test_family_totals_and_corpus_size_are_unchanged(self):
        self.assertEqual(len(self.qual), 1200)
        self.assertEqual(Counter(c.family for c in self.qual), Counter(frozen.COUNTS))
        self.assertEqual({f: len(self.email(f)) for f in EMAIL_SLOTS},
                         {"F05": 12, "F06": 15, "F16": 12})

    def test_every_other_scenario_is_byte_identical(self):
        kept = [c for c in self.qual if not _email_slot(c.family, index(c))]
        self.assertEqual(len(kept), 1161)
        self.assertEqual(digest(kept), QUAL_KEPT_SHA256)
        before = {c.scenario_id for c in corpus("dev", DEV_BEFORE_COUNTS, DEV_SEED)}
        self.assertEqual(digest([c for c in self.dev if c.scenario_id in before]), DEV_BEFORE_SHA256)
        self.assertEqual(sorted(c.scenario_id for c in self.dev if c.scenario_id not in before),
                         ["D-F05-0005", "D-F06-0004", "D-F16-0005"])
        self.assertTrue(all(c.connector == "email" for c in self.dev if c.scenario_id not in before))

    def test_email_is_never_declared_completable_by_redispatch(self):
        for case in self.email("F05") + self.email("F16"):
            self.assertFalse(case.completion_achievable, case.scenario_id)
        for case in self.email("F05"):
            self.assertEqual(case.operations[0].acceptable_terminal, ("escalated_unresolved",))
        for case in self.email("F06"):
            # The held request answers inside its 6 s deadline: the provider's
            # own response completes it, never a second send.
            self.assertEqual(case.contract_version, "q-v2-inflight")
            self.assertTrue(case.completion_achievable)

    def test_email_variants_are_spread_evenly(self):
        self.assertEqual(Counter(c.faults[0]["point"] for c in self.email("F05")),
                         {"after_provider_call": 4, "after_fence_commit": 4, "before_outcome_commit": 4})
        self.assertEqual(Counter(c.faults[0]["kind"] for c in self.email("F16")),
                         {"rate_limit": 6, "outage": 6})
        dev = {c.scenario_id: c for c in self.dev}
        self.assertEqual(dev["D-F05-0005"].faults[0]["point"], "after_provider_call")
        self.assertEqual(dev["D-F16-0005"].faults[0]["kind"], "rate_limit")

    def test_smoke_sample_keeps_one_email_cell_per_family(self):
        smoke = set(frozen.smoke_ids(self.qual))
        for family in EMAIL_SLOTS:
            self.assertEqual(len([c for c in self.email(family) if c.scenario_id in smoke]), 1)

    def test_corpus_is_disjoint_from_development(self):
        self.assertFalse({c.scenario_id for c in self.qual} & {c.scenario_id for c in self.dev})
        self.assertFalse({c.seed for c in self.qual} & {c.seed for c in self.dev})
        self.assertFalse({content(c) for c in self.qual} & {content(c) for c in self.dev})
        resources = lambda cases: {o.resource_id for c in cases for o in c.operations}
        self.assertFalse(resources(self.qual) & resources(self.dev))


if __name__ == "__main__":
    unittest.main()
