"""F13 coverage joins actual inverse requests, effects, lookups and DB samples."""

import json

from chaos.workflow_oracle import intent_matches, refund_precondition, representation_matches, snapshots, wire_keys
from connectors.http_trace import payload_hash


def observed(raw):
    from chaos.coverage import _pairs
    from chaos.scenarios import Scenario

    scenario_data = raw.get("scenario")
    if not scenario_data or not scenario_data.get("workflow"):
        return set()
    scenario = Scenario.from_json(json.dumps(scenario_data))
    spec, amount = scenario.workflow, scenario.operations[0].amount_minor
    ledger = [e for e in raw.get("ledger", []) if e.get("source") == "provider"]
    http = [e for e in raw.get("http", []) if e.get("source") == "http_client"]
    ppairs = _pairs(ledger, "provider_start", {"provider_end", "provider_error"}, "provider_call_id")
    hpairs = _pairs(http, "http_start", {"http_response", "http_error"}, "request_id")
    found = set()

    def joins(p, q, h, answer, key, method, path):
        return (all(e.get("provider") == "payments" and e.get("correlation_sha256") == key
                    for e in (p, q, h, answer))
            and h.get("method") == method and h.get("path") == path
            and h["monotonic_ns"] <= p["monotonic_ns"] <= q["monotonic_ns"] <= answer["monotonic_ns"])

    def unique_exchange(p, q, key, method, path):
        matches = [(h, answer) for h, answer in hpairs if joins(p, q, h, answer, key, method, path)]
        if len(matches) != 1:
            return None
        h, answer = matches[0]
        if sum(joins(a, b, h, answer, key, method, path) for a, b in ppairs) != 1:
            return None
        possible = [start for start in http if start.get("event") == "http_start"
            and start.get("provider") == "payments" and start.get("correlation_sha256") == key
            and start.get("method") == method and start.get("path") == path
            and start["monotonic_ns"] <= p["monotonic_ns"]
            and not any(end.get("event") in ("http_response", "http_error")
                and end.get("request_id") == start.get("request_id")
                and end["monotonic_ns"] < p["monotonic_ns"] for end in http)]
        if len(possible) != 1:
            return None
        starts = [e for e in ledger if e.get("event") == "provider_start"
            and e.get("provider") == "payments" and e.get("correlation_sha256") == key
            and e.get("method") == p.get("method")
            and h["monotonic_ns"] <= e["monotonic_ns"] <= answer["monotonic_ns"]]
        if len(starts) != 1:
            return None
        return h, answer

    for local_id in {a["local_id"] for a in raw.get("admissions", []) if a.get("local_id") and not a.get("changed")}:
        samples = snapshots(raw, local_id, timed=True)
        if not samples:
            continue
        steps = {s["step"]: s for s in samples[-1]["steps"]}
        if (len(steps) != len(samples[-1]["steps"]) or "charge" not in steps
                or not representation_matches(raw, samples[-1])
                or not intent_matches(samples[-1], spec, scenario.operations[0], steps)):
            continue
        charge = steps["charge"]
        if (charge.get("payload") != {"customer_id": spec.customer_id, "amount_minor": amount}
                or charge.get("action_type") != "charge_shipping_fee"):
            continue
        try:
            charge_keys = wire_keys(charge)
            refund = steps.get('compensation')
            if refund is None:
                continue
            refund_keys = wire_keys(refund)
        except (KeyError, ValueError, TypeError):
            continue
        charge_effects = [e for e in ledger if e.get("event") == "effect"
                         and e.get("correlation_sha256") in charge_keys]
        native = samples[-1].get('binding_format') in ('queue-request-v1', 'b0-request-v1')
        reference = charge.get('applied_reference') if native else (
            charge_effects[0].get('effect_id') if len(charge_effects) == 1 else None)
        if reference is None or reference not in {e.get('effect_id') for e in charge_effects}:
            continue
        expected = {"charge_id": reference, "amount_minor": amount}
        if (refund.get("payload") != expected or refund.get("action_type") != "refund_payment"
                or refund.get("compensates_operation_id") != charge["operation_id"]
                or refund["operation_id"] == charge["operation_id"] or charge_keys & refund_keys):
            continue
        if not all(e.get("payload_sha256") == payload_hash(expected) for e in ledger
                   if e.get("event") == "call" and e.get("correlation_sha256") in refund_keys):
            continue
        all_effects = [e for e in ledger if e.get("event") == "effect" and e.get("correlation_sha256") in refund_keys]
        for p, q in ppairs:
            key = p.get('correlation_sha256')
            if key not in refund_keys or p.get("method") != "refund" or q.get("event") != "provider_error":
                continue
            effects = [e for e in all_effects if e.get('correlation_sha256') == key
                and p['monotonic_ns'] <= e['monotonic_ns'] <= q['monotonic_ns']]
            exchange = unique_exchange(p, q, key, "POST", "/v1/refunds")
            for h, answer in [exchange] if exchange is not None else []:
                if (q.get("error_type") == "ProviderRejected" and not all_effects
                        and answer["event"] == "http_response" and answer.get("status_code") == 402
                        and refund_precondition(samples, charge, refund, q["monotonic_ns"])
                        and any(s["sample_started_ns"] > answer["monotonic_ns"]
                            and s["status"] == "COMPENSATION_UNRESOLVED"
                            and any(st["step"] == "compensation" and st["state"] == "FAILED_CONFIRMED"
                                    for st in s["steps"]) for s in samples)):
                    found.add("compensation_rejected_without_restoration")
                if (q.get("error_type") != "ProviderTimeout" or answer["event"] != "http_error"
                        or len(effects) != 1):
                    continue
                effect = effects[0]
                if not (p["monotonic_ns"] <= effect["monotonic_ns"] <= q["monotonic_ns"]
                        and refund_precondition(samples, charge, refund, effect["monotonic_ns"])):
                    continue
                found.add("compensation_applied_response_lost")
                if any(s["sample_started_ns"] > answer["monotonic_ns"]
                        and s["status"] == "COMPENSATION_UNRESOLVED"
                        and any(st["step"] == "compensation" and (st["state"] == "ESCALATED_UNRESOLVED"
                                or (native and st['state'] == 'UNKNOWN'))
                                for st in s["steps"]) for s in samples):
                    found.add("compensation_unresolved_after_lost_response")
                for lookup, response in ppairs:
                    if (lookup.get("method") != "lookup" or response["event"] != "provider_end"
                            or response.get("lookup_status") != "found"
                            or response.get("lookup_records") != [dict(effect_id=effect["effect_id"], payload_sha256=payload_hash(expected))]):
                        continue
                    exchange = unique_exchange(lookup, response, key, "GET", "/v1/lookup")
                    for get, got in [exchange] if exchange is not None else []:
                        if (get["monotonic_ns"] > answer["monotonic_ns"]
                                and joins(lookup, response, get, got, key, "GET", "/v1/lookup")
                                and got["event"] == "http_response" and got.get("status_code") == 200
                                and any(s["sample_started_ns"] > got["monotonic_ns"]
                                    and s["status"] == "COMPENSATED_VERIFIED"
                                    and any(st["step"] == "compensation" and st["state"] == "SUCCEEDED"
                                            for st in s["steps"]) for s in samples)):
                            found.add("compensation_verified_by_lookup")
    return found
