"""Compare all admitted operations with the independent provider ledger.

Unknown targets remain in the ledger and count as orphans. Every accepted
local id must be accounted for. Mixed local outcomes are reported explicitly;
without per-request effect attribution, a failed clone is not called a false
failure merely because another clone succeeded. Compensation scoring awaits
independent step/role evidence and is never inferred from an arm's class.
"""


def evaluate(scenario, raw):
    result = _evaluate(scenario, raw)
    # Scored from the stable snapshot; a later change is carried, never hidden.
    result["scored_from"] = raw.get("scored_from")
    result["unstable_at_scoring"] = list(raw.get("unstable_at_scoring") or [])
    return result


def _evaluate(scenario, raw):
    if scenario.workflow is not None:
        from chaos.workflow_oracle import evaluate as evaluate_workflow
        return evaluate_workflow(scenario, raw)
    submissions, first_submit = {}, {}
    for admission in raw["admissions"]:
        submissions.setdefault(admission["index"], []).append(admission)
    for event in raw["timeline"]:
        if event["event"] == "submission_started" and not event.get("changed"):
            index = event["index"]
            first_submit[index] = min(first_submit.get(index, event["at"]),
                                      event["at"])
    effects_by_resource, first_call = {}, {}
    timed_ledger = _observed_ledger(raw)
    for event in raw["ledger"]:
        resource = event.get("resource")
        if event["event"] == "effect":
            effects_by_resource[resource] = effects_by_resource.get(resource, 0) + 1
        elif event["event"] == "call":
            first_call[resource] = min(first_call.get(resource, event["at"]),
                                       event["at"])
    provider_metrics = _provider_metrics(timed_ledger)

    operations, accepted_resources = [], set()
    unaccounted = 0
    harness_errors = list(raw.get("harness_errors", []))
    if raw.get("arm") in ("safeact","b3") and scenario.family in ("F14", "F15"):
        from chaos.boundary_coverage import observed, required
        missing = required(scenario) - observed(raw)
        if missing:
            harness_errors.append("Missing boundary observations: " + ", ".join(sorted(missing)))
    for index, spec in enumerate(scenario.operations):
        mine = submissions.get(index, [])
        expected = spec.submits + int(spec.changed_resubmit)
        if len(mine) != expected:
            harness_errors.append(f"operation {index}: {len(mine)}/{expected} admissions")
        local_ids = sorted({a["local_id"] for a in mine if a["local_id"]})
        changed_ids = {a["local_id"] for a in mine
                       if a["local_id"] and a["changed"]}
        views = [raw["operations"].get(local_id) for local_id in local_ids]
        missing = sum(v is None or v.get("state") == "MISSING" for v in views)
        unaccounted += missing
        views = [v for v in views if v is not None and v.get("state") != "MISSING"]
        if local_ids:
            accepted_resources.add(spec.resource_id)
        effects = effects_by_resource.get(spec.resource_id, 0)
        classes = sorted({v["outcome_class"] for v in views})
        outcome = (classes[0] if len(classes) == 1 else
                   "mixed" if classes else "not_accepted" if not local_ids
                   else "unaccounted")
        start = first_submit.get(index, raw["started"])
        resolved = [v["resolved_at"] for v in views if v.get("resolved_at") is not None]
        escalated = [v["escalated_at"] for v in views if v.get("escalated_at") is not None]
        acceptable = tuple(spec.acceptable_terminal)
        correct_effects = ((outcome == "confirmed_success" and effects == 1)
                           or (outcome == "confirmed_failure" and effects == 0))
        operations.append({
            "index": index, "resource_id": spec.resource_id,
            "accepted_operations": len(local_ids), "changed_accepted": len(changed_ids),
            "effects": effects, "prohibited_duplicates": max(0, effects - 1),
            "outcome_class": outcome, "local_outcome_classes": classes,
            "acceptable_terminal": acceptable,
            "false_failure_claim": classes == ["confirmed_failure"] and effects > 0,
            "false_success_claim": "confirmed_success" in classes and effects == 0,
            "safe_completion": bool(views) and not missing and not changed_ids
                               and outcome in acceptable and correct_effects,
            "resolution_seconds": _since(start, max(resolved) if resolved
                                          and len(resolved) == len(local_ids) else None),
            "escalation_seconds": _since(start, min(escalated) if escalated else None),
            "queue_wait_seconds": _since(start, first_call.get(spec.resource_id)),
            **provider_metrics.get(spec.resource_id, _empty_provider_metrics()),
            "evidence_rows": sum(v.get("evidence_rows", 0) for v in views),
        })

    orphans = sum(n for resource, n in effects_by_resource.items()
                  if resource not in accepted_resources)
    result = {
        "scenario_id": scenario.scenario_id, "family": scenario.family,
        "connector": scenario.connector, "arm": raw["arm"],
        "completion_achievable": scenario.completion_achievable,
        "expected_operations": len(scenario.operations),
        "operations": operations,
        "prohibited_duplicates": sum(o["prohibited_duplicates"] for o in operations),
        "orphan_effects": orphans, "unaccounted": unaccounted,
        "changed_accepted": sum(o["changed_accepted"] for o in operations),
        "false_claims": sum(o["false_failure_claim"] + o["false_success_claim"]
                            for o in operations),
        "integrity": list(raw.get("integrity") or []),
        "harness_errors": harness_errors,
        "cleanup_incomplete": bool(raw.get("cleanup_incomplete")),
        "seconds": round(raw.get("elapsed_seconds", raw["finished"] - raw["started"]), 3),
    }
    if safety_failures(result) or harness_errors or result["cleanup_incomplete"]:
        for operation in operations:
            operation["safe_completion"] = False
    return result


def _observed_ledger(raw):
    cutoff = raw.get("observation_finished_monotonic_ns")
    return [event for event in raw.get("ledger", [])
            if cutoff is None or (
                type(event.get("monotonic_ns")) is int
                and event["monotonic_ns"] <= cutoff)]


def _empty_provider_metrics():
    return {
        "provider_dispatch_calls": 0,
        "provider_lookup_calls": 0,
        "provider_calls": 0,
        "provider_call_cost_usd": 0.0,
        "provider_cost_basis": "mock_only_zero_external_cost",
        "verification_delay_seconds": None,
    }


def _provider_metrics(events):
    """Derive request and verification timing from observed mock calls only."""

    dispatch_methods = {"refund", "charge", "create_record", "send"}
    lookup_methods = {"lookup", "search"}
    grouped = {}
    for event in events:
        if event.get("source") != "provider":
            continue
        resource = event.get("resource")
        if not resource:
            continue
        grouped.setdefault(resource, []).append(event)

    result = {}
    ambiguous = {
        "ProviderTimeout", "ProviderServerError", "ConnectionError",
        "CrmTimeout", "CrmServerError", "EmailTimeout", "EmailServerError",
    }
    for resource, rows in grouped.items():
        rows.sort(key=lambda event: event.get("monotonic_ns", 0))
        calls = [event for event in rows if event.get("event") == "provider_start"]
        failures = [event for event in rows
                    if event.get("event") == "provider_error"
                    and event.get("method") in dispatch_methods
                    and event.get("error_type") in ambiguous
                    and type(event.get("monotonic_ns")) is int]
        verified = [event for event in rows
                    if event.get("event") == "provider_end"
                    and event.get("method") in lookup_methods
                    and type(event.get("monotonic_ns")) is int]
        first_ambiguity = min(
            (event["monotonic_ns"] for event in failures), default=None,
        )
        first_verification = min(
            (event["monotonic_ns"] for event in verified
             if first_ambiguity is not None
             and event["monotonic_ns"] > first_ambiguity),
            default=None,
        )
        delay = ((first_verification - first_ambiguity) / 1_000_000_000
                 if first_verification is not None else None)
        dispatch_count = sum(event.get("method") in dispatch_methods for event in calls)
        lookup_count = sum(event.get("method") in lookup_methods for event in calls)
        result[resource] = {
            "provider_dispatch_calls": dispatch_count,
            "provider_lookup_calls": lookup_count,
            "provider_calls": dispatch_count + lookup_count,
            "provider_call_cost_usd": 0.0,
            "provider_cost_basis": "mock_only_zero_external_cost",
            "verification_delay_seconds": delay,
        }
    return result


def safety_failures(row):
    return bool(any(row.get(key, 0) for key in (
        "prohibited_duplicates", "orphan_effects", "unaccounted",
        "changed_accepted", "false_claims", "compensation_duplicates",
        "invalid_compensation_effects")) or row.get("integrity"))


def _since(start, at):
    if at is None or start is None:
        return None
    # Negative measurements expose clock/order problems instead of hiding them.
    return round(at - start, 3)
