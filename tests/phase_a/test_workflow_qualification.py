"""Workflow evidence counterexamples; no database or provider sockets."""

import copy
import json
import unittest
import threading

from unittest.mock import patch

from chaos import oracle, qualify, scoring
from chaos.scenarios import Scenario, generate
from chaos.workflow_faults import WorkflowPayments
from connectors.http_trace import correlation_hash, payload_hash
from connectors.ledger import EffectsLedger
from connectors.payments_mock import ProviderRejected, ProviderTimeout


def fixture(variant=0):
    case = generate("dev", "F13", variant, 7)
    spec = case.workflow
    amount = case.operations[0].amount_minor
    payloads = {"charge": {"customer_id": spec.customer_id, "amount_minor": amount},
        "order": {"case_id": spec.case_id, "sku": spec.sku},
        "compensation": {"charge_id": "charge-effect", "amount_minor": amount}}
    actions = {"charge": "charge_shipping_fee", "order": "create_replacement_order", "compensation": "refund_payment"}
    steps = []
    for role in ("charge", "order", "compensation"):
        steps.append(dict(step=role, operation_id=f"op-{role}",
            compensates_operation_id="op-charge" if role == "compensation" else None,
            connector_id="mock-crm" if role == "order" else "mock-payments",
            action_type=actions[role], resource_id=spec.case_id if role == "order" else spec.customer_id,
            provider_account_id="account", contract_version=getattr(spec, f"{role}_contract"),
            payload=payloads[role], correlation_sha256=correlation_hash(role),
            state="FAILED_CONFIRMED" if role == "order" else "SUCCEEDED",
            transition_seq=3, evidence_rows=1, state_changed_at=108,
            resolved_at=108, escalated_at=None))
    plan = dict(order=dict(resource_id=spec.case_id, payload_canonical=json.dumps(payloads["order"])),
                compensation=dict(policy="refund_charge_when_order_failed_confirmed"))
    def sample(ns):
        return dict(source="database", event="db_workflow_sample", workflow_id="workflow",
            sample_started_ns=ns-1, sample_finished_ns=ns, monotonic_ns=ns,
            plan=copy.deepcopy(plan), steps=copy.deepcopy(steps), status="COMPENSATED_VERIFIED")
    before, final = sample(28), sample(40)
    before["steps"][-1]["state"]="DISPATCHING"
    ledger=[]
    for role, method, ns in (("charge","charge",10),("order","create_record",20),("compensation","refund",30)):
        key=correlation_hash(role)
        ledger += [dict(event="provider_start", source="provider", monotonic_ns=ns-1, at=101,
                       correlation_sha256=key, provider="crm" if role=="order" else "payments",method=method),
                   dict(event="call",source="provider",monotonic_ns=ns,at=102,
                       correlation_sha256=key,payload_sha256=payload_hash(payloads[role]))]
        if role != "order":
            ledger.append(dict(event="effect",source="provider",monotonic_ns=ns+1,at=103,
                correlation_sha256=key,effect_id="charge-effect" if role=="charge" else "refund-effect"))
    raw=dict(scenario_id=case.scenario_id, arm="safeact", started=100, finished=110,
        observation_finished_monotonic_ns=100,
        admissions=[dict(index=0,copy=0,changed=False,local_id="workflow",refused=None)],
        operations={"workflow":dict(state="COMPENSATED_VERIFIED", outcome_class="verified_compensation",
            resolved_at=109,escalated_at=None,evidence_rows=3)},
        ledger=ledger, database=[before,final],
        timeline=[dict(event="submission_started",index=0,changed=False,at=100)],integrity=[])
    return case, raw


class WorkflowOracleTests(unittest.TestCase):
    def test_inverse_is_separate_from_forward_effect_and_does_not_count_as_duplicate(self):
        case, raw=fixture()
        row=oracle.evaluate(case,raw)
        self.assertFalse(oracle.safety_failures(row),row)
        self.assertTrue(row["operations"][0]["safe_completion"])
        self.assertEqual((row["forward_effects"],row["compensation_effects"],row["prohibited_duplicates"]),(1,1,0))
        self.assertEqual([s["effects"] for s in row["operations"][0]["steps"]],[1,0,1])

    def test_duplicate_inverse_is_a_safety_failure_separate_from_forward_duplicates(self):
        case, raw=fixture()
        raw["ledger"].append(dict(raw["ledger"][-1],effect_id="second-refund",monotonic_ns=32))
        row=oracle.evaluate(case,raw)
        self.assertEqual(row["prohibited_duplicates"],0)
        self.assertEqual(row["compensation_duplicates"],1)
        self.assertEqual(qualify.exit_code([row],1),1)
        self.assertFalse(row["operations"][0]["safe_completion"])

    def test_wrong_target_amount_link_or_method_cannot_prove_restoration(self):
        for mutation in ("target","amount","link","method","contract"):
            with self.subTest(mutation=mutation):
                case,raw=fixture()
                for sample in raw["database"]:
                    comp=sample["steps"][-1]
                    if mutation=="target":comp["payload"]["charge_id"]="unrelated-charge"
                    elif mutation=="amount":comp["payload"]["amount_minor"]+=1
                    elif mutation=="link":comp["compensates_operation_id"]="other-operation"
                    elif mutation=="contract":comp["contract_version"]="wrong-contract"
                if mutation=="method":
                    next(e for e in raw["ledger"] if e["event"]=="provider_start" and e["method"]=="refund")["method"]="charge"
                # Even internally consistent forged payload bindings must
                # be compared with the scenario and the real charge id.
                if mutation in ("target","amount"):
                    raw["ledger"][-2]["payload_sha256"]=payload_hash(raw["database"][-1]["steps"][-1]["payload"])
                row=oracle.evaluate(case,raw)
                self.assertTrue(oracle.safety_failures(row))
                self.assertEqual(row["invalid_compensation_effects"],1)
                self.assertEqual(row["orphan_effects"],1)
                self.assertEqual(row["false_claims"],1)

    def test_post_effect_confirmation_does_not_prove_the_refund_was_authorized_when_applied(self):
        case,raw=fixture()
        raw["database"][0]["steps"][1]["state"]="UNKNOWN"
        row=oracle.evaluate(case,raw)
        self.assertEqual(row["invalid_compensation_effects"],1)
        self.assertFalse(row["operations"][0]["safe_completion"])

    def test_missing_inverse_or_cleanup_only_inverse_is_a_false_restoration_claim(self):
        for cleanup_only in (False,True):
            case,raw=fixture()
            if cleanup_only:raw["ledger"][-1]["monotonic_ns"]=101
            else:raw["ledger"].pop()
            row=oracle.evaluate(case,raw)
            self.assertEqual(row["false_claims"],1)
            self.assertFalse(row["operations"][0]["safe_completion"])

    def test_missing_committed_bindings_and_unattributed_effects_are_not_dropped(self):
        case,raw=fixture();raw["database"]=[]
        row=oracle.evaluate(case,raw)
        self.assertEqual((row["unaccounted"],row["orphan_effects"]),(1,2))
        self.assertTrue(row["harness_errors"])
        case,raw=fixture();raw["ledger"].append(dict(raw["ledger"][-1],correlation_sha256="unknown",effect_id="orphan"))
        self.assertEqual(oracle.evaluate(case,raw)["orphan_effects"],1)

    def test_shared_identities_and_changed_workflow_plan_fail_safety(self):
        for mutation in ("operation_id","correlation_sha256","plan"):
            case,raw=fixture()
            for sample in raw["database"]:
                if mutation=="plan":sample["plan"]["order"]["resource_id"]="other-case"
                else:sample["steps"][-1][mutation]=sample["steps"][0][mutation]
            self.assertTrue(oracle.safety_failures(oracle.evaluate(case,raw)))

    def test_unresolved_refund_preserves_forward_and_inverse_history_without_false_claim(self):
        case,raw=fixture(1)
        raw["operations"]["workflow"].update(state="COMPENSATION_UNRESOLVED",outcome_class="escalated_unresolved",resolved_at=None,escalated_at=109)
        raw["database"][-1]["steps"][-1]["state"]="ESCALATED_UNRESOLVED"
        row=oracle.evaluate(case,raw)
        self.assertFalse(oracle.safety_failures(row),row)
        self.assertEqual((row["forward_effects"],row["compensation_effects"]),(1,1))
        self.assertFalse(row["operations"][0]["safe_completion"])

    def test_workflow_counts_and_inverse_duplicates_survive_aggregation(self):
        case,raw=fixture()
        row=oracle.evaluate(case,raw)
        counts=scoring.aggregate([row])
        self.assertEqual((counts["effects"],counts["forward_effects"],counts["compensation_effects"]),(2,1,1))
        self.assertEqual(counts["usefulness"],dict(completed=1,achievable=1,fraction=1.0))
        raw["ledger"].append(dict(raw["ledger"][-1],effect_id="second-refund",monotonic_ns=32))
        counts=scoring.aggregate([oracle.evaluate(case,raw)])
        self.assertEqual(counts["compensation_duplicates"],1)
        self.assertEqual(counts["safety_failure_rows"],1)
        self.assertEqual(counts["usefulness"]["completed"],0)

    def test_missing_order_effect_and_duplicate_charge_cannot_be_called_completed(self):
        case,raw=fixture(4)
        raw["ledger"]=[e for e in raw["ledger"] if e.get("correlation_sha256")!=correlation_hash("compensation")]
        raw["ledger"].append(dict(raw["ledger"][2],correlation_sha256=correlation_hash("order"),effect_id="order-effect",monotonic_ns=21))
        for sample in raw["database"]:
            sample["steps"].pop()
            sample["steps"][-1]["state"]="SUCCEEDED"
            sample["status"]="COMPLETED"
        raw["operations"]["workflow"].update(state="COMPLETED",outcome_class="confirmed_success")
        self.assertTrue(oracle.evaluate(case,raw)["operations"][0]["safe_completion"])
        for mutation in ("missing_order","duplicate_charge"):
            altered=copy.deepcopy(raw)
            if mutation=="missing_order":altered["ledger"].pop()
            else:altered["ledger"].append(dict(raw["ledger"][2],effect_id="second-charge",monotonic_ns=12))
            row=oracle.evaluate(case,altered)
            self.assertEqual(row["false_claims"],1)
            self.assertFalse(row["operations"][0]["safe_completion"])
            if mutation=="duplicate_charge":self.assertEqual(row["prohibited_duplicates"],1)


def lost_response_trace():
    case, raw=fixture()
    raw["scenario"]=json.loads(case.to_json())
    key=correlation_hash("compensation")
    def event(name, ns, **fields):
        return dict(event=name,monotonic_ns=ns,provider="payments",correlation_sha256=key,**fields)
    raw["ledger"][-3]["provider_call_id"]="refund"
    payload=raw["database"][-1]["steps"][-1]["payload"]
    raw["ledger"] += [event("provider_error",35,source="provider",provider_call_id="refund",method="refund",error_type="ProviderTimeout"),
        event("provider_start",45,source="provider",provider_call_id="lookup",method="lookup"),
        event("provider_end",47,source="provider",provider_call_id="lookup",method="lookup",lookup_status="found",
            lookup_records=[dict(effect_id="refund-effect",payload_sha256=payload_hash(payload))])]
    raw["http"]=[event("http_start",27,source="http_client",request_id="post",method="POST",path="/v1/refunds"),
        event("http_error",36,source="http_client",request_id="post",method="POST",path="/v1/refunds"),
        event("http_start",44,source="http_client",request_id="get",method="GET",path="/v1/lookup"),
        event("http_response",48,source="http_client",request_id="get",method="GET",path="/v1/lookup",status_code=200)]
    raw["database"][-1].update(sample_started_ns=49,sample_finished_ns=50,monotonic_ns=50)
    return raw


class WorkflowCoverageTests(unittest.TestCase):
    def test_actual_refund_loss_then_matching_lookup_and_committed_restoration(self):
        from chaos.coverage import observed
        found=observed(lost_response_trace())
        self.assertIn("compensation_applied_response_lost",found)
        self.assertIn("compensation_verified_by_lookup",found)

    def test_wrong_lookup_or_missing_http_cannot_prove_verified_compensation(self):
        from chaos.coverage import observed
        for mutation in ("reference","payload","http","db_time"):
            raw=lost_response_trace()
            if mutation=="reference":raw["ledger"][-1]["lookup_records"][0]["effect_id"]="other-refund"
            elif mutation=="payload":raw["ledger"][-1]["lookup_records"][0]["payload_sha256"]="other-payload"
            elif mutation=="http":raw["http"].pop()
            else:raw["database"][-1].update(sample_started_ns=46,sample_finished_ns=47,monotonic_ns=47)
            self.assertNotIn("compensation_verified_by_lookup",observed(raw),mutation)

    def test_uncertain_order_or_cleanup_effect_never_receives_inverse_coverage(self):
        from chaos.coverage import observed
        for mutation in ("order","cleanup","identity"):
            raw=lost_response_trace()
            if mutation=="order":raw["database"][0]["steps"][1]["state"]="UNKNOWN"
            elif mutation=="cleanup":
                next(e for e in raw["ledger"] if e.get("effect_id")=="refund-effect")["monotonic_ns"]=101
            else:raw["database"][-1]["steps"][-1]["operation_id"]="op-charge"
            self.assertNotIn("compensation_applied_response_lost",observed(raw),mutation)

    def test_ambiguous_or_unfinished_http_exchanges_receive_no_inverse_coverage(self):
        from chaos.coverage import observed
        for unfinished in (False,True):
            raw=lost_response_trace()
            raw["http"] += [dict(e,request_id="other-post") for e in copy.deepcopy(raw["http"][:1 if unfinished else 2])]
            self.assertNotIn("compensation_applied_response_lost",observed(raw))
        raw=lost_response_trace()
        raw["http"] += [dict(e,request_id="other-get") for e in copy.deepcopy(raw["http"][2:])]
        self.assertNotIn("compensation_verified_by_lookup",observed(raw))
        raw=lost_response_trace()
        start=next(e for e in raw["ledger"] if e.get("provider_call_id")=="refund")
        raw["ledger"].append(dict(start,provider_call_id="unfinished",monotonic_ns=34))
        self.assertNotIn("compensation_applied_response_lost",observed(raw))


class WorkflowFaultTests(unittest.TestCase):
    def test_observer_failure_is_retained_even_when_a_provider_translates_the_exception(self):
        from chaos.arms.safeact import SafeActArm
        arm=SafeActArm()
        arm._broker_ids_lock=threading.Lock()
        arm._workflow_ids={"workflow"}
        arm._workflow_observation_errors=[]
        with patch("chaos.arms.workflows.local_admin_settings",return_value={}), \
             patch("chaos.arms.workflows.psycopg.connect",side_effect=ConnectionError("observer unavailable")):
            with self.assertRaises(ConnectionError):arm.sample_workflows_before_refund()
        self.assertEqual(arm.trace_errors(),["Workflow DB observer failed: ConnectionError"])

    def test_refund_reply_fault_never_consumes_the_initial_charge(self):
        ledger=EffectsLedger();provider=WorkflowPayments(ledger=ledger);samples=[]
        provider.configure_workflow("lost_response",lambda:samples.append("db"))
        charge=provider.charge("charge",{"customer_id":"customer","amount_minor":10})
        with self.assertRaises(ProviderTimeout):provider.refund("refund",{"charge_id":charge.refund_id,"amount_minor":10})
        replay=provider.refund("refund",{"charge_id":charge.refund_id,"amount_minor":10})
        self.assertTrue(replay.replayed)
        self.assertEqual(len(ledger.effects_for("charge")),1)
        self.assertEqual(len(ledger.effects_for("refund")),1)
        self.assertEqual(samples,["db","db"])

    def test_hidden_refund_loses_replay_replies_while_native_key_preserves_one_effect(self):
        ledger=EffectsLedger();provider=WorkflowPayments(ledger=ledger)
        provider.configure_workflow("hidden_response",lambda:None)
        for _ in range(3):
            with self.assertRaises(ProviderTimeout):provider.refund("refund",{"charge_id":"original","amount_minor":10})
        self.assertEqual(provider.lookup("refund").status,"absent")
        self.assertEqual(len(ledger.effects_for("refund")),1)

    def test_rejection_logs_a_real_request_without_an_inverse_effect(self):
        ledger=EffectsLedger();provider=WorkflowPayments(ledger=ledger)
        provider.configure_workflow("rejected",lambda:None)
        with self.assertRaises(ProviderRejected):provider.refund("refund",{"charge_id":"original","amount_minor":10})
        self.assertEqual(len(ledger.calls_for("refund")),1)
        self.assertEqual(ledger.effects_for("refund"),())

    def test_f13_variants_serialize_an_actual_workflow_and_explicit_expected_terminal(self):
        for index in range(5):
            case=generate("dev","F13",index,7)
            self.assertEqual(len(case.operations),1)
            self.assertIsNotNone(case.workflow)
            self.assertEqual(Scenario.from_json(case.to_json()),case)
            self.assertEqual(case.workflow.customer_id,case.operations[0].resource_id)


if __name__=="__main__":
    unittest.main()
