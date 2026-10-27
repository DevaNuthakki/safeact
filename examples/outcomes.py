"""Summarize saved development results without running providers or qualification.

    .venv/bin/python -m examples.outcomes docs/phase-e/dev-workflows-20261022/results.jsonl --family F13
"""

import argparse
import json
from pathlib import Path

from chaos.scoring import summarize


def render(rows, *, family=None):
    if family:
        rows = [row for row in rows if row["family"] == family]
    if not rows:
        raise ValueError("No matching results")
    summary = summarize(rows)
    lines = ["# Customer outcomes — development evidence", "",
        "Mock providers; small development sample; scoring is not frozen. Full qualification and B0–B3 comparisons are NOT_RUN.", "",
        "A workflow counts as one requested unit. A confirmed refund is a recovery outcome, not a completed replacement.", "",
        "| Arm | Requested units | Useful / achievable | Confirmed success | Confirmed compensation | Unresolved | Error rows |",
        "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for arm, counts in summary["by_arm"].items():
        outcomes = counts["outcomes"]
        usefulness = counts["usefulness"]
        lines.append(f"| {arm} | {counts['applicable_operations']} | {usefulness['completed']}/{usefulness['achievable']} | "
            f"{outcomes.get('confirmed_success', 0)} | {outcomes.get('verified_compensation', 0)} | "
            f"{outcomes.get('escalated_unresolved', 0)} | {counts['rows_with_errors']} |")
        latency = counts["latency_seconds"]["resolution_seconds"]
        lines += ["", f"{arm}: {counts['prohibited_duplicates']} prohibited forward duplicates, "
            f"{counts['compensation_duplicates']} duplicate refunds, {counts['invalid_compensation_effects']} invalid refunds, "
            f"{counts['orphan_effects']} orphan effects and {counts['false_claims']} false outcome claims observed.", "",
            f"Provider activity: {counts['provider_calls']} calls ({counts['provider_dispatch_calls']} dispatch, "
            f"{counts['provider_lookup_calls']} lookup). Resolution latency: p50 {latency['p50']} s, "
            f"p95 {latency['p95']} s across {latency['count']} measured units "
            f"({latency['invalid']} invalid measurements).", "",
            f"N/A rows: {counts['not_applicable']}; excluded from requested units and completion counts.", ""]
        other = {name: count for name, count in outcomes.items()
                 if name not in {"confirmed_success", "verified_compensation", "escalated_unresolved"}}
        if other:
            lines += ["Other recorded outcomes: " + ", ".join(
                f"{name.replace('_', ' ')} = {count}" for name, count in sorted(other.items())) + ".", ""]
    lines += ["Unresolved outcomes indicate investigation is needed; these are not counts of human reviews performed.", "",
        "Useful completion includes confirmed compensation where declared achievable. Refusals and errors stay in the achievable denominator.", "",
        "Latency covers measured resolutions only, not every request or the time spent unresolved; it is not full workload latency.", "",
        "Provider calls show activity, not comparative overhead: no baseline is available yet. No estimate of money saved or duplicates prevented is made.", "",
        summary["provider_cost_method"], ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results", type=Path)
    parser.add_argument("--family")
    args = parser.parse_args(argv)
    rows = [json.loads(line) for line in args.results.read_text().splitlines() if line.strip()]
    try:
        print(render(rows, family=args.family), end="")
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
