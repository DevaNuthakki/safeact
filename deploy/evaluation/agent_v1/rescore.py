"""Score a committed results file under both scoring rules.

    .venv/bin/python -m deploy.evaluation.agent_v1.rescore RESULTS_DIR

Reads RESULTS_DIR/results.jsonl as produced (never edited) and writes
RESULTS_DIR/rescored.json and rescored.md: every trial scored with the
original strict rule (agent-eval-scoring-v1) and the current rule
(agent-eval-scoring-v2), per-arm totals for both, and each trial whose
verdict differs.
"""

import json
import sys

from pathlib import Path

from deploy.evaluation.agent_v1 import report as reporting
from deploy.evaluation.agent_v1 import scenarios, scoring


def rescore(directory):
    directory = Path(directory)
    rows = [json.loads(line) for line in
            (directory / "results.jsonl").read_text("utf-8").splitlines() if line.strip()]
    by_id = {s["scenario_id"]: s for split in scenarios.SPLITS
             for s in scenarios.load(split)}
    versions = (scoring.STRICT_VERSION, scoring.SCORING_VERSION)
    scored = {v: [{**r, "score": scoring.score(r, by_id[r["scenario_id"]], version=v)}
                  for r in rows] for v in versions}
    totals = {v: {arm: reporting.summarize([r for r in scored[v] if r["arm"] == arm])
                  for arm in reporting.ARMS if any(r["arm"] == arm for r in rows)}
              for v in versions}
    changed = [
        {"scenario_id": a["scenario_id"], "arm": a["arm"], "trial": a["trial"],
         "report": a["score"]["report"],
         versions[0]: a["score"]["correct_business_outcome"],
         versions[1]: b["score"]["correct_business_outcome"]}
        for a, b in zip(scored[versions[0]], scored[versions[1]])
        if a["score"]["correct_business_outcome"] != b["score"]["correct_business_outcome"]
    ]
    result = {"versions": list(versions), "totals": totals, "changed": changed}
    (directory / "rescored.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    lines = [f"# {directory.name}: scored under both rules", "",
             f"Raw results: `results.jsonl` (unedited). Rules: `{versions[0]}` "
             f"(strict: only each scenario's listed reports) and `{versions[1]}` "
             "(in every scenario whose expected outcome is escalation, "
             "unresolved or needs_human_review is correct, for all arms; never "
             "success).", "",
             "| Arm | Trials | Correct (v1 strict) | Correct (v2) | Justified esc. | Unjustified esc. |",
             "|---|---|---|---|---|---|"]
    for arm in totals[versions[1]]:
        a, b = totals[versions[0]][arm], totals[versions[1]][arm]
        lines.append(f"| {arm} | {b['trials']} | {a['correct_business_outcome']} | "
                     f"{b['correct_business_outcome']} | {b['justified_escalations']}/"
                     f"{b['escalation_expected']} | {b['unjustified_escalations']} |")
    lines += ["", f"## Trials whose verdict differs ({len(changed)})", ""]
    lines += [f"- `{c['scenario_id']}` {c['arm']} #{c['trial']}: report={c['report']}, "
              f"v1={c[versions[0]]}, v2={c[versions[1]]}" for c in changed] or ["None."]
    (directory / "rescored.md").write_text("\n".join(lines) + "\n")
    return result


def main(argv=None):
    for directory in (argv if argv is not None else sys.argv[1:]):
        print(json.dumps(rescore(directory)["changed"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
