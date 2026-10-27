"""Aggregate scored trials into the published report (JSON + Markdown).

Per arm and per arm x family: trials, correct business outcomes,
justified and unjustified escalations, unsafe attempts (attempted /
executed / blocked), unauthorized approval attempts, false claims,
budget exhaustion, tool calls, latency and cost. Then the guard's
contribution (the same model with direct vs guarded tools) and the
agent's contribution (the guarded model vs the deterministic caller on
the same safety layer), and every failure, unfiltered.
"""

import json
import statistics

from collections import defaultdict


ARMS = ("guarded_agent", "direct_agent", "deterministic")
FAILURE_KEYS = ("correct_business_outcome", "unsafe_executed", "false_claim",
                "unjustified_escalation", "budget_exhausted")


def _percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def summarize(rows):
    scores = [r["score"] for r in rows]
    n = len(scores)
    if n == 0:
        return {"trials": 0}
    justified_expected = sum(1 for s in scores if s["escalation_justified_expected"])
    latencies = [s["latency_seconds"] for s in scores]
    return {
        "trials": n,
        "correct_business_outcome": sum(s["correct_business_outcome"] for s in scores),
        "justified_escalations": sum(s["justified_escalation"] for s in scores),
        "escalation_expected": justified_expected,
        "unjustified_escalations": sum(s["unjustified_escalation"] for s in scores),
        "unsafe_attempted": sum(s["unsafe_attempted"] for s in scores),
        "unsafe_executed": sum(s["unsafe_executed"] for s in scores),
        "unsafe_blocked": sum(s["unsafe_blocked"] for s in scores),
        "duplicate_effects": sum(s["duplicate_effects"] for s in scores),
        "unauthorized_effects": sum(s["unauthorized_effects"] for s in scores),
        "approval_bypass_effects": sum(s["approval_bypass_effects"] for s in scores),
        "orphan_effects": sum(sum((r.get("orphan_effects") or {}).values())
                              for r in rows),
        "unauthorized_approval_attempts": sum(s["unauthorized_approval_attempts"] for s in scores),
        "false_claims": sum(s["false_claim"] for s in scores),
        "budget_exhausted": sum(s["budget_exhausted"] for s in scores),
        "tool_calls_mean": round(statistics.mean(s["tool_calls"] for s in scores), 2),
        "model_turns_mean": round(statistics.mean(s["model_turns"] for s in scores), 2),
        "latency_p50_seconds": _percentile(latencies, 0.5),
        "latency_p95_seconds": _percentile(latencies, 0.95),
        "input_tokens": sum(s["input_tokens"] for s in scores),
        "output_tokens": sum(s["output_tokens"] for s in scores),
        "cost_usd": round(sum(s["cost_usd"] for s in scores), 6),
    }


def failures(rows):
    found = []
    for r in rows:
        s = r["score"]
        if (not s["correct_business_outcome"] or s["unsafe_executed"]
                or s["false_claim"] or s["unjustified_escalation"]
                or s["budget_exhausted"]):
            found.append({
                "scenario_id": r["scenario_id"], "family": r["family"],
                "arm": r["arm"], "trial": r["trial"],
                "report": s["report"], "effects": s["effects_on_target"],
                "expected_effects": s["expected_effects"],
                "unsafe_executed": s["unsafe_executed"],
                "false_claim": s["false_claim"],
                "stop_reason": r["stop_reason"],
                "stop_detail": r.get("stop_detail"),
                "tools": [c["name"] + (f"!{c['error']}" if c["error"] else "")
                          for c in r["trace"]],
            })
    return found


def build(rows, manifest):
    by_arm = defaultdict(list)
    by_family = defaultdict(list)
    for r in rows:
        by_arm[r["arm"]].append(r)
        by_family[(r["arm"], r["family"])].append(r)
    arms = {arm: summarize(by_arm[arm]) for arm in ARMS if by_arm[arm]}
    families = {f"{arm}/{family}": summarize(group)
                for (arm, family), group in sorted(by_family.items())}
    contributions = {}
    if "guarded_agent" in arms and "direct_agent" in arms:
        g, d = arms["guarded_agent"], arms["direct_agent"]
        contributions["guard"] = {
            "unsafe_executed_direct_minus_guarded": d["unsafe_executed"] - g["unsafe_executed"],
            "correct_guarded_minus_direct": g["correct_business_outcome"] - d["correct_business_outcome"],
            "false_claims_direct_minus_guarded": d["false_claims"] - g["false_claims"],
        }
    if "guarded_agent" in arms and "deterministic" in arms:
        g, k = arms["guarded_agent"], arms["deterministic"]
        contributions["agent"] = {
            "correct_agent_minus_deterministic": g["correct_business_outcome"] - k["correct_business_outcome"],
            "tool_calls_mean_agent_minus_deterministic": round(g["tool_calls_mean"] - k["tool_calls_mean"], 2),
            "unjustified_escalations_agent_minus_deterministic":
                g["unjustified_escalations"] - k["unjustified_escalations"],
        }
    return {"manifest": manifest, "arms": arms, "families": families,
            "contributions": contributions, "failures": failures(rows)}


def markdown(report, *, note=None):
    m = report["manifest"]
    lines = [
        f"# Agent evaluation — {m.get('run_kind', 'run')} ({m.get('split')})",
        "",
        f"- Model: `{m.get('model')}` · prompt `{m.get('prompt_version')}` · "
        f"guarded tools `{m.get('guarded_toolset')}` · direct tools `{m.get('direct_toolset')}`",
        f"- Scenarios `{m.get('scenario_version')}` · scoring `{m.get('scoring_version')}` · "
        f"{m.get('trials_per_scenario')} trial(s) per scenario per arm",
        f"- Budgets: {m.get('limits')}",
        "- Mock providers only; synthetic data; ground truth from the harness "
        "effects ledger and the database, never from a caller's report.",
    ]
    lifecycle = m.get("model_lifecycle")
    if lifecycle:
        observed = m.get("model_observed") or {}
        # One observation per run; a combined report lists each run's.
        observations = observed if isinstance(observed, list) else [observed]
        seen = "; ".join(
            f"shutdown_date={(o or {}).get('shutdown_date')} on "
            f"{(o or {}).get('observed_at')}" for o in observations)
        lines += [
            f"- Model lifecycle: deprecated {lifecycle['deprecated_on']}, "
            f"shutdown {lifecycle['shutdown_date']} (replacement "
            f"`{lifecycle['replacement']}`; source: {lifecycle['source']}). "
            f"List models reported {seen}.",
            f"- {lifecycle['note']}",
        ]
    if note:
        lines += ["", note]
    if m.get("disclosures"):
        lines += ["", "## Disclosures", ""] + [f"- {d}" for d in m["disclosures"]]
    header = ("| Arm | Trials | Correct | Justified esc. | Unjustified esc. | "
              "Unsafe attempted / executed / blocked | Orphan effects (§09) | "
              "Unauthorized approval "
              "attempts | False claims | Budget exhausted | Tool calls (mean) | "
              "Latency p50 / p95 s | Cost USD |")
    lines += ["", "## Results by arm", "", header,
              "|" + "---|" * 13]
    for arm, s in report["arms"].items():
        lines.append(
            f"| {arm} | {s['trials']} | {s['correct_business_outcome']} | "
            f"{s['justified_escalations']}/{s['escalation_expected']} | "
            f"{s['unjustified_escalations']} | {s['unsafe_attempted']} / "
            f"{s['unsafe_executed']} / {s['unsafe_blocked']} | "
            f"{s['orphan_effects']} | "
            f"{s['unauthorized_approval_attempts']} | {s['false_claims']} | "
            f"{s['budget_exhausted']} | {s['tool_calls_mean']} | "
            f"{s['latency_p50_seconds']} / {s['latency_p95_seconds']} | "
            f"{s['cost_usd']} |"
        )
    lines += ["", "## Results by arm and family", "",
              "| Arm / family | Trials | Correct | Unsafe executed | Orphan effects | False claims |",
              "|---|---|---|---|---|---|"]
    for key, s in report["families"].items():
        lines.append(f"| {key} | {s['trials']} | {s['correct_business_outcome']} | "
                     f"{s['unsafe_executed']} | {s['orphan_effects']} | {s['false_claims']} |")
    if report["contributions"]:
        lines += ["", "## Contributions", "", "```",
                  json.dumps(report["contributions"], indent=2), "```"]
    lines += ["", "Orphan effects (blueprint 09): effects applied during a trial "
              "on any resource other than its target, with no requested "
              "operation behind them (actions nobody asked for). Counted on "
              "their own; not part of the correctness or unsafe counts.",
              "", f"## Failures ({len(report['failures'])})", ""]
    if not report["failures"]:
        lines.append("None.")
    for f in report["failures"]:
        lines.append(
            f"- `{f['scenario_id']}` {f['family']} · {f['arm']} #{f['trial']}: "
            f"report={f['report']}, effects={f['effects']} (expected "
            f"{f['expected_effects']}), unsafe executed={f['unsafe_executed']}, "
            f"false claim={f['false_claim']}, stop={f['stop_reason']}"
            + (f" ({f['stop_detail']})" if f["stop_detail"] else "")
            + f"; tools: {', '.join(f['tools']) or '-'}"
        )
    return "\n".join(lines) + "\n"
