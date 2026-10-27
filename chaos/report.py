"""Readable development evidence; exit status alone is not a completion result."""

import json


def render(summary):
    lines = ["# SafeAct "+('qualification' if summary['scoring_frozen'] else 'development')+" evidence", "", summary["scope"], "",
        "Safety failures and useful completion are reported separately. A zero exit status checks",
        "harness validity and SafeAct safety; it does not assert useful completion or full coverage.", "",
        "Useful completion counts requested operations whose declared acceptable terminal and",
        "independent effect count agree, divided by all operations declared achievable.", "",
        "A requested workflow is one completion unit; its forward steps and inverse retain separate",
        "identities, evidence and effect counts in the raw results and per-step scores.", "",
        "Refusals and errored cases stay in that denominator. N/A rows are excluded and counted separately.", "",
        "The final ledger includes effects drained during cleanup. Coverage and completion use the",
        "observation window; cleanup releases do not establish a scheduled interleaving.", "",
        "Rows are scored from the snapshot that passed the stability check. A row whose state",
        "changed in a later snapshot (window close or final) keeps both states and is counted as",
        "unstable at scoring.", "",
        "Declared trade-off: the attempt budget is fixed. A short provider outage can spend every",
        "attempt, so SafeAct escalates with 0 effects where a luckier retry timing completes; retry",
        "timing comes from per-operation jitter, so this varies between runs. A candidate improvement",
        "for a later version (not evaluated): do not count attempts the transport proves were never",
        "sent, or make attempt accounting breaker-aware.", ""]
    for title, dimension in (("By arm", "by_arm"), ("By family", "by_family"), ("By connector", "by_connector")):
        lines += ["## " + title, "",
            "| Group | Useful / achievable | Safety-failure rows | Error rows | Unstable at scoring | Effects | Duplicates | Orphans | N/A rows |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for name, counts in summary[dimension].items():
            useful = counts["usefulness"]
            lines.append(f"| {name} | {useful['completed']}/{useful['achievable']} | "
                f"{counts['safety_failure_rows']} | {counts['rows_with_errors']} | "
                f"{counts.get('unstable_at_scoring_rows', 0)} | {counts['effects']} | "
                f"{counts['prohibited_duplicates']} | {counts['orphan_effects']} | {counts['not_applicable']} |")
        lines.append("")
    lines += ["## Outcomes by arm", ""]
    for name, counts in summary["by_arm"].items():
        lines.append(f"- {name}: " + json.dumps(counts["outcomes"], sort_keys=True))
    lines += ["", "Effect attribution by arm:", ""]
    for name, counts in summary["by_arm"].items():
        lines.append(f"- {name}: {counts['forward_effects']} forward, "
            f"{counts['compensation_effects']} compensation, {counts['orphan_effects']} orphan; "
            f"{counts['compensation_duplicates']} duplicate compensation effects and "
            f"{counts['invalid_compensation_effects']} invalid compensation effects.")
    lines += ["", "Provider activity and cost basis:", ""]
    for name, counts in summary["by_arm"].items():
        lines.append(
            f"- {name}: {counts['provider_calls']} mock calls "
            f"({counts['provider_dispatch_calls']} dispatch, "
            f"{counts['provider_lookup_calls']} lookup); "
            f"external provider cost ${counts['provider_call_cost_usd']:.2f}."
        )
    lines += ["", summary["provider_cost_method"], "",
        "Latency distributions (including verification delay) and database-evidence row counts are in summary.json.", "",
        ("Scoring is frozen to the verified input manifest." if summary['scoring_frozen'] else
         "Scoring is not frozen. Pending: " + "; ".join(summary["pending_metrics"]) + "."), ""]
    return "\n".join(lines)
