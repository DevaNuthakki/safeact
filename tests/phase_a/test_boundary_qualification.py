"""Counterexamples for review/agent qualification evidence joins (offline)."""

import json
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

from chaos import boundary_coverage as coverage, oracle, qualify
from chaos.scenarios import Scenario, generate
from chaos.arms.base import Admission, OperationView
from chaos.runner import run
from connectors.ledger import EffectsLedger


def timed(ns, **fields):
    return dict(sample_started_ns=ns, sample_finished_ns=ns+1, monotonic_ns=ns+1, **fields)


def with_lookup(raw):
    successful=min(s["sample_started_ns"] for s in raw["database"] if s["state"]=="SUCCEEDED")
    for event in raw["ledger"]:
        if event["event"]=="effect":event["effect_id"]="effect"
    raw["ledger"].append(dict(source="provider",event="call",payload_sha256="payload",correlation_sha256="key",monotonic_ns=1))
    common=dict(source="provider",provider="crm",method="search",path="/records",provider_call_id="lookup",correlation_sha256="key")
    raw["ledger"] += [dict(common,event="provider_start",monotonic_ns=successful-2),
        dict(common,event="provider_end",monotonic_ns=successful-1,lookup_records=[dict(effect_id="effect",payload_sha256="payload")])]
    http=dict(source="http_client",provider="crm",method="GET",path="/records",request_id="get",correlation_sha256="key")
    raw["http"]=[dict(http,event="http_start",monotonic_ns=successful-3),
                 dict(http,event="http_response",monotonic_ns=successful-1,status_code=200)]
    return raw


def review_fixture(mode="concurrent"):
    case = generate("dev", "F14", ("concurrent", "stale", "protected_retry").index(mode), 7)
    principals = [dict(principal_id=p, kind="human", roles=[role], tenant_id="north")
                  for p, role in (("requester", "requester"), ("investigator", "investigator"),
                                  ("r1", "reviewer"), ("r2", "reviewer"))]
    retry = mode == "protected_retry"
    proposal = dict(proposal_id="proposal", proposed_by="investigator", action="PROTECTED_RETRY" if retry else "REVERIFY",
                    bound_seq=5, bound_state="VERIFYING" if retry else "ESCALATED_UNRESOLVED", binding_digest="digest")
    evidence = [dict(outcome="proven_not_applied" if retry else "may_have_dispatched", provenance="system")]
    def snap(ns, label, **changes):
        return timed(ns, source="database", event="db_boundary_snapshot", label=label, operation_id="op",
            state=changes.pop("state", proposal["bound_state"]), seq=changes.pop("seq", 5), correlation_sha256="key",
            requester_id="requester", principals=principals, proposals=[proposal], decisions=changes.pop("decisions", []),
            evidence=changes.pop("evidence", evidence), audit=changes.pop("audit", []), **changes)
    calls = [timed(10+i*2, event="proposal_displayed", source="boundary_client", actor_id=f"r{i+1}",
             proposal_id="proposal", status_code=200, binding_digest="digest") for i in range(2)]
    displayed = snap(20, "displayed_binding")
    ledger = [dict(source="provider",event="provider_start",method="create_record", correlation_sha256="key",monotonic_ns=1)]
    if retry:
        ledger += [dict(source="provider",event="provider_error",error_type="CrmValidationRejected",correlation_sha256="key",monotonic_ns=2),
                   dict(source="provider",event="provider_start",method="create_record",correlation_sha256="key",monotonic_ns=41)]
    ledger.append(dict(source="provider",event="effect",correlation_sha256="key",monotonic_ns=42 if retry else 2))
    samples = [displayed]
    if mode == "stale":
        samples.append(snap(30, "changed", state="SUCCEEDED",seq=6,evidence=[dict(outcome="confirmed_applied",provenance="system")]))
        calls.append(timed(40, event="decision_response", actor_id="r1", proposal_id="proposal",binding_digest="digest",
                           status_code=409,error="stale_approval",decision_key="key1"))
        samples.append(snap(50,"stale_refused",state="SUCCEEDED",seq=6,audit=[dict(event_type="APPROVAL_REFUSED_STALE",
            actor_id="r1",commit_xid="15",details=dict(proposal_id="proposal",bound_seq=5,current_seq=6))]))
    else:
        for actor, ns, status, outcome, error in (("r1",30,200,"approved",None),("r2",31,409,None,"already_decided")):
            calls.append(dict(timed(ns,event="decision_response",actor_id=actor,proposal_id="proposal",binding_digest="digest",
                status_code=status,outcome=outcome,error=error,decision_key="key"+actor[-1]),sample_finished_ns=40,monotonic_ns=40))
        calls.append(timed(35,source="database",event="reviewers_blocked",operation_id="op",proposal_id="proposal",
            api_backend_pids=[2,3],blocker_pid=1,blocking_edges=[dict(pid=2,blocked_by=[1]),dict(pid=3,blocked_by=[2])]))
        calls.append(timed(45,event="decision_replayed",status_code=200,outcome="replayed",proposal_id="proposal",actor_id="r1",decision_key="key1"))
        samples.append(snap(50,"race_decided",decisions=[dict(proposal_id="proposal",decided_by="r1",decision="approve",decision_key="key1")],
            audit=[dict(event_type="DECISION_RECORDED",actor_id="r1",commit_xid="15",details=dict(proposal_id="proposal",bound_seq=5))]))
        samples.append(snap(60,"review_complete",state="SUCCEEDED",seq=7))
    return case, with_lookup(dict(arm="safeact",scenario_id=case.scenario_id,scenario=json.loads(case.to_json()),
        observation_finished_monotonic_ns=100, admissions=[dict(local_id="op", index=0,copy=0,changed=False)],
        database=samples,boundary=calls,ledger=ledger))


def agent_fixture():
    case = generate("dev","F15",0,7)
    principals = [dict(principal_id=p,kind="service",roles=["requester","investigator"],tenant_id=t)
                  for p,t in (("agent","north"),("other","south"))]
    keys = [dict(key_id=k,principal_id=p,scopes=["submit","investigate"],revoked=k=="k1")
            for k,p in (("k1","agent"),("k2","agent"),("k3","other"))]
    proposals=[dict(proposal_id="proposal",proposed_by="agent",action="REVERIFY")]
    ns=20
    def sample(label, at):
        return timed(at,source="database",event="db_boundary_snapshot",label=label,operation_id="op",state="ESCALATED_UNRESOLVED",
            seq=5,correlation_sha256="key",requester_id="agent",principals=principals,agent_keys=keys,proposals=proposals,
            decisions=[],evidence=[dict(outcome="may_have_dispatched")],operation_ids=["op"],provider_dispatches=1,provider_effects=1)
    calls=[]
    for i in range(4):
        calls.append(dict(timed(5,event="agent_admission",key_id="k1" if i%2==0 else "k2",operation_id="op",is_error=False),
                          sample_finished_ns=10,monotonic_ns=10))
    for i in range(2):
        calls.append(timed(11+i*2,event="credential_replay",key_id=f"k{i+1}",operation_id="op",is_error=False))
    samples=[]
    for name,error in coverage.ATTACKS.items():
        samples.append(sample(name+"_before",ns))
        calls.append(timed(ns+2,event=name,is_error=True,error=error,
            key_id="k3" if name.startswith("cross_tenant_") else "k1" if name=="revoked_credential" else "k2"))
        samples.append(sample(name+"_after",ns+4))
        ns+=8
    calls.append(timed(ns,event="agent_proposal",is_error=False,operation_id="op",proposal_id="proposal",key_id="k2"))
    ns+=4
    for form in ("bearer","cookie"):
        samples.append(sample("human_endpoint_"+form+"_before",ns))
        calls.append(timed(ns+2,event="self_approval_human_"+form,status_code=401,key_id="k2",proposal_id="proposal"))
        samples.append(sample("human_endpoint_"+form+"_after",ns+4))
        ns+=8
    final=sample("agent_complete",ns)
    final["state"]="SUCCEEDED"
    samples.append(final)
    ledger=[dict(source="provider",event="provider_start",method="create_record",correlation_sha256="key",monotonic_ns=1),
            dict(source="provider",event="effect",correlation_sha256="key",monotonic_ns=2)]
    return case, with_lookup(dict(observation_finished_monotonic_ns=ns+10,admissions=[dict(local_id="op")],database=samples,boundary=calls,ledger=ledger))


class BoundaryQualificationTests(unittest.TestCase):
    def test_review_modes_are_explicit_deterministic_and_roundtrip(self):
        for i, mode in enumerate(("concurrent","stale","protected_retry")):
            case=generate("dev","F14",i,7)
            self.assertEqual(case.review_mode, mode)
            self.assertEqual(Scenario.from_json(case.to_json()),case)
            self.assertTrue(case.completion_achievable)
            self.assertFalse(any(f.get("switch")=="drop_from_index" for f in case.faults))
        self.assertEqual(generate("dev","F15",0,7).operations[0].submits,4)

    def test_blocking_chain_includes_indirect_waiters_and_refuses_missing_edges(self):
        event=dict(api_backend_pids=[2,3],blocker_pid=1,blocking_edges=[dict(pid=2,blocked_by=[1]),dict(pid=3,blocked_by=[2])])
        self.assertTrue(coverage._blocking_chain(event))
        event["blocking_edges"].pop()
        self.assertFalse(coverage._blocking_chain(event))

    def test_real_observations_qualify_each_review_mode(self):
        for mode,label in (("concurrent","reviewers_serialized_on_binding"),("stale","stale_review_refused_after_evidence"),
                           ("protected_retry","reviewed_retry_after_proven_nonapplication")):
            self.assertEqual(coverage.observed(review_fixture(mode)[1]),{label})

    def test_scheduled_race_without_two_overlapping_blocked_requests_is_not_coverage(self):
        for mutation in ("no_block", "missing_edge", "nonoverlap", "same_reviewer", "same_key"):
            with self.subTest(mutation=mutation):
                _,raw=review_fixture()
                blocked=next(c for c in raw["boundary"] if c["event"]=="reviewers_blocked")
                responses=[c for c in raw["boundary"] if c["event"]=="decision_response"]
                if mutation=="no_block":raw["boundary"].remove(blocked)
                elif mutation=="missing_edge":blocked["blocking_edges"].pop()
                elif mutation=="nonoverlap":responses[1]["sample_started_ns"]=40
                elif mutation=="same_reviewer":responses[1]["actor_id"]="r1"
                else:responses[1]["decision_key"]=responses[0]["decision_key"]
                self.assertFalse(coverage.observed(raw))

    def test_response_codes_without_one_committed_decision_audit_and_replay_do_not_qualify(self):
        for mutation in ("decision", "audit", "replay", "second_decision", "effect", "extra_dispatch", "human"):
            with self.subTest(mutation=mutation):
                _,raw=review_fixture()
                decided=raw["database"][1]
                if mutation=="decision":decided["decisions"]=[]
                elif mutation=="audit":decided["audit"]=[]
                elif mutation=="replay":raw["boundary"]=[c for c in raw["boundary"] if c["event"]!="decision_replayed"]
                elif mutation=="second_decision":decided["decisions"].append(dict(decided["decisions"][0]))
                elif mutation=="effect":raw["ledger"].pop()
                elif mutation=="extra_dispatch":raw["ledger"].append(dict(raw["ledger"][0],monotonic_ns=3))
                else:raw["database"][0]["principals"][2]["kind"]="service"
                self.assertFalse(coverage.observed(raw))

    def test_stale_refusal_needs_new_committed_provider_evidence_before_the_request(self):
        for mutation in ("unchanged", "late", "no_audit", "decided"):
            _,raw=review_fixture("stale")
            if mutation=="unchanged":raw["database"][1]["evidence"]=raw["database"][0]["evidence"]
            elif mutation=="late":raw["database"][1].update(timed(45))
            elif mutation=="no_audit":raw["database"][-1]["audit"]=[]
            else:raw["database"][-1]["decisions"]=[dict(proposal_id="proposal")]
            self.assertFalse(coverage.observed(raw),mutation)

    def test_retry_needs_real_definitive_rejection_before_approval(self):
        _,raw=review_fixture("protected_retry")
        raw["ledger"]=[e for e in raw["ledger"] if e["event"]!="provider_error"]
        self.assertFalse(coverage.observed(raw))

    def test_cleanup_only_observations_never_qualify(self):
        _,raw=review_fixture()
        raw["observation_finished_monotonic_ns"]=39
        self.assertFalse(coverage.observed(raw))

    def test_agent_boundary_fixture_qualifies_all_three_independent_claims(self):
        self.assertEqual(coverage.observed(agent_fixture()[1]),{
            "agent_credential_rotation_collapsed","agent_self_approval_refused","agent_boundary_attacks_refused"})

    def test_rotation_needs_same_principal_keys_and_overlapping_tool_calls(self):
        for mutation in ("principal","no_overlap","same_key","no_replay","scope"):
            _,raw=agent_fixture()
            events=[c for c in raw["boundary"] if c["event"]=="agent_admission"]
            if mutation=="principal":raw["database"][-1]["agent_keys"][1]["principal_id"]="other"
            elif mutation=="no_overlap":
                for i,e in enumerate(events):e.update(timed(5+i*2))
            elif mutation=="same_key":
                for e in events:e["key_id"]="k1"
            elif mutation=="scope":raw["database"][-1]["agent_keys"][1]["scopes"]=["investigate"]
            else:raw["boundary"]=[c for c in raw["boundary"] if c["event"]!="credential_replay"]
            self.assertNotIn("agent_credential_rotation_collapsed",coverage.observed(raw),mutation)

    def test_every_agent_attack_needs_refusal_and_unchanged_independent_snapshots(self):
        for name in coverage.ATTACKS:
            with self.subTest(name=name):
                _,raw=agent_fixture()
                next(s for s in raw["database"] if s["label"]==name+"_after")["seq"]+=1
                self.assertNotIn("agent_boundary_attacks_refused",coverage.observed(raw))
                _,raw=agent_fixture()
                next(c for c in raw["boundary"] if c["event"]==name)["is_error"]=False
                self.assertNotIn("agent_boundary_attacks_refused",coverage.observed(raw))

    def test_human_endpoint_refusal_requires_no_decision_evidence_or_provider_effect(self):
        for field,value in (("decisions",[dict(decision="approve")]),("evidence",[]),("provider_effects",2)):
            _,raw=agent_fixture()
            next(s for s in raw["database"] if s["label"]=="human_endpoint_bearer_after")[field]=value
            self.assertNotIn("agent_self_approval_refused",coverage.observed(raw))

    def test_other_principal_in_same_tenant_does_not_prove_tenant_isolation(self):
        _,raw=agent_fixture()
        raw["database"][-1]["principals"][1]["tenant_id"]="north"
        self.assertNotIn("agent_boundary_attacks_refused",coverage.observed(raw))

    def test_saved_provider_counts_must_match_the_independent_ledger(self):
        _,raw=agent_fixture()
        raw["ledger"].append(dict(next(e for e in raw["ledger"] if e["event"]=="effect"),monotonic_ns=3))
        self.assertFalse(coverage.observed(raw))

    def test_ambiguity_resolution_needs_a_real_successful_lookup_of_the_effect(self):
        for fixture in (review_fixture,agent_fixture):
            for mutation in ("no_http","wrong_effect","wrong_payload","failed_http","late_lookup"):
                _,raw=fixture()
                if mutation=="no_http":raw["http"]=[]
                elif mutation in ("wrong_effect","wrong_payload"):
                    answer=next(e for e in raw["ledger"] if e["event"]=="provider_end")
                    answer["lookup_records"][0]["effect_id" if mutation=="wrong_effect" else "payload_sha256"]="unrelated"
                elif mutation=="failed_http":raw["http"][-1]["status_code"]=503
                else:raw["http"][-1]["monotonic_ns"]=raw["observation_finished_monotonic_ns"]+1
                self.assertFalse(coverage.observed(raw),(fixture,mutation))

    def test_missing_boundary_work_fails_qualification_even_with_one_correct_effect(self):
        case,raw=review_fixture()
        raw.update(started=100,finished=110,timeline=[],integrity=[],
            operations={"op":dict(state="SUCCEEDED",outcome_class="confirmed_success",evidence_rows=1,resolved_at=109)})
        for event in raw["ledger"]:
            event.update(at=101,resource=case.operations[0].resource_id)
        clean=oracle.evaluate(case,raw)
        self.assertFalse(clean["harness_errors"])
        self.assertTrue(clean["operations"][0]["safe_completion"])
        raw["boundary"]=[c for c in raw["boundary"] if c["event"]!="reviewers_blocked"]
        missing=oracle.evaluate(case,raw)
        self.assertFalse(oracle.safety_failures(missing))
        self.assertEqual(qualify.exit_code([missing],1),1)
        self.assertIn("Missing boundary observations",missing["harness_errors"][0])
        self.assertFalse(missing["operations"][0]["safe_completion"])

    def test_driver_failure_preserves_admissions_and_timed_operation_accounting(self):
        class Arm:
            name="safeact"
            def prepare(self, scenario): pass
            def submit(self, *args, **kwargs): return Admission("op")
            def drive_boundary(self, *args): raise RuntimeError("review observer failed")
            def observe(self, ids): return {i:OperationView(i,"confirmed_success","SUCCEEDED") for i in ids}
            def quiesce(self): pass
            def check_integrity(self): return []
        with tempfile.TemporaryDirectory() as directory:
            slot=SimpleNamespace(index=0,ledger=EffectsLedger(),fault_dir=Path(directory),reset=lambda:None,
                close_provider_admission=lambda:None,wait_provider_idle=lambda timeout:None)
            raw=run(slot,Arm(),generate("dev","CONTROL",0,7))
        self.assertEqual(raw["admissions"][0]["local_id"],"op")
        self.assertIn("op",raw["operations"])
        self.assertIn("review observer failed",raw["harness_errors"][0])
        self.assertFalse(raw["cleanup_incomplete"])


if __name__ == "__main__":
    unittest.main()
