"""Development aggregates: safety and useful completion have separate measures.

Denominators include requested operations in errored and refused cases. N/A
rows are counted separately and never counted as successes. Workload-level
achievability and all-requested completion are reported independently.
"""

import math

from collections import Counter

from chaos import oracle

SCORING_VERSION = 3


def distribution(values):
    valid = sorted(v for v in values if isinstance(v, (int, float))
                   and not isinstance(v, bool) and math.isfinite(v) and v >= 0)
    def percentile(p):
        return valid[max(0, math.ceil(len(valid) * p) - 1)] if valid else None
    return {"count": len(valid), "invalid": len(values) - len(valid),
            "p50": percentile(0.5), "p95": percentile(0.95),
            "maximum": valid[-1] if valid else None}


def aggregate(rows):
    result = dict(scenario_arms=len(rows), not_applicable=0, applicable_operations=0,
        accepted_local_ids=0, rows_with_errors=0, safety_failure_rows=0,
        effects=0, prohibited_duplicates=0, orphan_effects=0, unaccounted=0,
        changed_accepted=0, false_claims=0, operations_with_database_evidence=0,
        forward_effects=0, compensation_effects=0, compensation_duplicates=0,
        invalid_compensation_effects=0, unstable_at_scoring_rows=0)
    outcomes = Counter()
    safely_completed_requested = 0
    achievable = completed = 0
    latencies = {key: [] for key in (
        "resolution_seconds", "escalation_seconds", "queue_wait_seconds",
        "verification_delay_seconds",
    )}
    result.update(provider_dispatch_calls=0, provider_lookup_calls=0,
                  provider_calls=0, provider_call_cost_usd=0.0,
                  provider_cost_basis="mock_only_zero_external_cost")
    for row in rows:
        if row.get("not_applicable"):
            result["not_applicable"] += 1
            continue
        expected = row.get("expected_operations")
        operations = row.get("operations", [])
        if type(expected) is not int or expected < len(operations) or expected < 1:
            raise ValueError("Missing or invalid requested-operation denominator")
        if type(row.get("completion_achievable")) is not bool:
            raise ValueError("Missing achievable-completion declaration")
        indices = [operation["index"] for operation in operations]
        if len(set(indices)) != len(indices) or any(type(i) is not int or not 0 <= i < expected for i in indices):
            raise ValueError("Duplicate or invalid operation index")
        errored = bool(row.get("harness_errors") or row.get("cleanup_incomplete")
                       or len(operations) != expected)
        unsafe = oracle.safety_failures(row)
        result["rows_with_errors"] += errored
        result["safety_failure_rows"] += unsafe
        result["unstable_at_scoring_rows"] += bool(row.get("unstable_at_scoring"))
        result["applicable_operations"] += expected
        for key in ("prohibited_duplicates", "orphan_effects", "unaccounted", "changed_accepted", "false_claims",
                    "compensation_duplicates", "invalid_compensation_effects"):
            result[key] += row.get(key, 0)
        result["effects"] += row.get("orphan_effects", 0)
        outcomes["unaccounted"] += expected - len(operations)
        if row["completion_achievable"]:
            achievable += expected
        for operation in operations:
            outcomes[operation["outcome_class"]] += 1
            safely_completed_requested += bool(operation.get('safe_completion')) and not errored and not unsafe
            result["accepted_local_ids"] += operation["accepted_operations"]
            result["effects"] += operation["effects"]
            result["forward_effects"] += operation.get("forward_effects", operation["effects"])
            result["compensation_effects"] += operation.get("compensation_effects", 0)
            result["operations_with_database_evidence"] += operation.get("evidence_rows", 0) > 0
            for key in ("provider_dispatch_calls", "provider_lookup_calls", "provider_calls"):
                result[key] += operation.get(key, 0)
            result["provider_call_cost_usd"] += operation.get("provider_call_cost_usd", 0.0)
            if operation.get("provider_cost_basis", result["provider_cost_basis"]) != result["provider_cost_basis"]:
                raise ValueError("Mixed provider-cost bases are not supported")
            if row["completion_achievable"] and not errored and not unsafe:
                completed += bool(operation["safe_completion"])
            for key, values in latencies.items():
                if operation.get(key) is not None:
                    values.append(operation[key])
    result["outcomes"] = dict(sorted((key, value) for key, value in outcomes.items() if value))
    result["usefulness"] = {"completed": completed, "achievable": achievable,
        "fraction": completed / achievable if achievable else None}
    result['all_requested_completion'] = dict(completed=safely_completed_requested,
        requested=result['applicable_operations'], fraction=(safely_completed_requested/result['applicable_operations']
            if result['applicable_operations'] else None))
    result["latency_seconds"] = {key: distribution(values) for key, values in latencies.items()}
    return result


def summarize(rows):
    pairs = [(row["scenario_id"], row["arm"]) for row in rows]
    if len(set(pairs)) != len(pairs):
        raise ValueError("Duplicate scenario/arm result")
    summary = {"scope": "development only; qualification incomplete", "scoring_frozen": False,
               'scoring_version': SCORING_VERSION}
    for dimension, field in (("by_arm", "arm"), ("by_family", "family"), ("by_connector", "connector")):
        summary[dimension] = {name: aggregate([r for r in rows if r[field] == name])
                              for name in sorted({r[field] for r in rows})}
    summary["pending_metrics"] = ['STOP 2 review and post-review slot calibration']
    summary["latency_method"] = "nearest-rank percentiles; invalid measurements counted separately"
    summary["provider_cost_method"] = (
        "Mock qualification providers have no external charge; provider calls are counted "
        "from provider-side observations and cost is recorded as $0.00, not extrapolated "
        "to Stripe or other real providers."
    )
    return summary
