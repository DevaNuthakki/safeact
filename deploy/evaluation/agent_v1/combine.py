"""Combine runs made under one frozen manifest into one report.

    .venv/bin/python -m deploy.evaluation.agent_v1.combine OUT RUN_DIR [RUN_DIR ...]

Each run's results.jsonl is read as produced (never edited). Refuses
unless every run's frozen manifest fields are identical (everything but
the run-specific run_id, worst_case_usd, model_trials, trial_indices,
model_observed) and no (scenario, arm, trial index) appears twice, so a
combined report can never mix configurations or count a trial twice.
Writes OUT/results.jsonl (the union, each row tagged with its run),
report.json, report.md and the both-rules rescore.
"""

import json
import sys

from pathlib import Path

from deploy.evaluation.agent_v1 import report as reporting
from deploy.evaluation.agent_v1 import scenarios, scoring
from deploy.evaluation.agent_v1.rescore import rescore


RUN_SPECIFIC = {"run_id", "worst_case_usd", "model_trials", "trial_indices",
                "model_observed"}


def frozen(manifest):
    return {k: v for k, v in manifest.items() if k not in RUN_SPECIFIC}


def combine(out, run_dirs, *, note=None):
    manifests, rows, seen = [], [], set()
    for directory in map(Path, run_dirs):
        manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
        manifests.append(manifest)
        for line in (directory / "results.jsonl").read_text("utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            key = (row["scenario_id"], row["arm"], row["trial"])
            if key in seen:
                raise SystemExit(f"Refused: trial {key} appears in more than one run")
            seen.add(key)
            rows.append({**row, "run_id": manifest["run_id"]})
    if any(frozen(m) != frozen(manifests[0]) for m in manifests[1:]):
        raise SystemExit("Refused: the runs were not made under one frozen manifest")

    by_id = {s["scenario_id"]: s for split in scenarios.SPLITS
             for s in scenarios.load(split)}
    for row in rows:
        row["score"] = scoring.score(row, by_id[row["scenario_id"]])
    combined = {**frozen(manifests[0]),
                "run_kind": manifests[0]["run_kind"] + " (combined)",
                "combined_runs": [m["run_id"] for m in manifests],
                "trials_per_scenario": len({r["trial"] for r in rows}),
                "model_observed": [m.get("model_observed") for m in manifests]}
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "results.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    report = reporting.build(rows, combined)
    (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True,
                                                default=str) + "\n")
    (out / "report.md").write_text(reporting.markdown(report, note=note))
    (out / "manifest.json").write_text(json.dumps(combined, indent=2,
                                                  sort_keys=True, default=str) + "\n")
    rescore(out)
    return report


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    combine(argv[0], argv[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
