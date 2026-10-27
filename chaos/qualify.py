"""Run only development scenarios against mock providers (M5.9 in progress).

Requires SAFEACT_RUN_QUALIFICATION=1. Every (scenario, arm) gets a fresh
slot and arm environment. This command cannot execute the frozen corpus.
Raw results, explicit errors and incomplete coverage are retained for review.
"""

import argparse
import importlib
import json
import os
import queue
import sys
import tempfile
import threading
import time
import traceback

from pathlib import Path

from chaos import SLOT_REDIS_DBS, coverage, environment, oracle, report, scoring
from chaos.runner import run
from chaos.scenarios import FAMILIES, corpus
from chaos.applicability import critical_labels, reason as applicability_reason


DEV_SEED = 7
DEV_COUNTS = {family: 3 for family in FAMILIES}
DEV_COUNTS["F13"] = 5  # Real fixed workflows: verified/unresolved inverse, rejection and controls.
DEV_COUNTS["F06"] = 5  # Payments and CRM from the seed; index 4 is the email slot.
DEV_COUNTS["F05"] = 6  # Before send, after provider reply, before outcome commit; 5 is email.
DEV_COUNTS["F11"] = 6  # All three commit boundaries, including a non-retryable fence.
DEV_COUNTS["F08"] = 15  # Eight flushes, four early redeliveries and three isolated Redis kills.
DEV_COUNTS["F07"] = 5   # Preserve three late-lookup cases; add two post-escalation commits.
DEV_COUNTS["F16"] = 6   # Three storms/outages, two deadline crossings; 5 is email.


def arms_named(names):
    if not names or any(not name for name in names) or len(names) != len(set(names)):
        raise SystemExit("--arms must contain distinct, nonempty names")
    from chaos.arms.safeact import SafeActArm

    available = {"safeact": SafeActArm}
    try:
        baselines = importlib.import_module("chaos.arms.baselines")
        available.update(baselines.ARMS)
    except ModuleNotFoundError as error:
        if error.name != "chaos.arms.baselines":
            raise
    unknown = set(names) - set(available)
    if unknown:
        raise SystemExit(f"unknown arms: {sorted(unknown)}")
    return [available[name] for name in names]


def scenarios_for(args):
    if args.corpus != "dev":
        raise SystemExit("only --corpus dev until STOP 2 review")
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be positive")
    if args.families is not None:
        wanted = {name.strip() for name in args.families.split(",")}
        unsupported = wanted - set(DEV_COUNTS)
        if unsupported:
            raise SystemExit(f"unsupported development families: {sorted(unsupported)}")
    else:
        wanted = set(DEV_COUNTS)
    found = corpus("dev", {f: n for f, n in DEV_COUNTS.items() if f in wanted}, DEV_SEED)
    if getattr(args, "cases", None) is not None:
        chosen = [name.strip() for name in args.cases.split(",")]
        if (not all(chosen) or len(set(chosen)) != len(chosen)
                or set(chosen) - {s.scenario_id for s in found}):
            raise SystemExit("--cases must name distinct existing development cases in the selected families")
        found = [s for s in found if s.scenario_id in chosen]
    if not found:
        raise SystemExit("No scenarios selected")
    return found[:args.limit] if args.limit is not None else found


def make_slot(index, **options):
    from chaos.slot import Slot
    return Slot(index, **options)


def slot_worker(index, arm_classes, work, out, lock, workdir, errors, *, control=None):
    while True:
        try:
            scenario = work.get_nowait()
        except queue.Empty:
            return
        for arm_class in arm_classes:
            if control is not None and not control.allow():
                return
            arm_name = arm_class.name
            raw = {"scenario_id": scenario.scenario_id, "arm": arm_name,
                   "scenario": json.loads(scenario.to_json()), "harness_errors": []}
            slot = arm = None
            not_applicable_reason=applicability_reason(arm_class,scenario)
            if not_applicable_reason is not None:
                raw["not_applicable"] = True
                raw['not_applicable_reason']=not_applicable_reason
                evaluated = {"scenario_id": scenario.scenario_id, "arm": arm_name,
                             "not_applicable": True,'not_applicable_reason':not_applicable_reason}
            else:
                try:
                    isolated = any(f["kind"] == "broker_kill" for f in scenario.faults)
                    slot = make_slot(index, isolated_broker=True) if isolated else make_slot(index)
                    slot.start()
                    arm = arm_class()
                    arm.setup(slot, workdir / f"slot{index}" / scenario.scenario_id / arm_name)
                    raw.update(run(slot, arm, scenario))
                    raw["contract_version"] = scenario.contract_version
                except Exception:
                    raw["harness_errors"].append(traceback.format_exc())
                finally:
                    for owner in (arm, slot):
                        if owner is not None:
                            try:
                                owner.teardown() if owner is arm else owner.stop()
                            except Exception:
                                raw["harness_errors"].append(traceback.format_exc())
                                raw["cleanup_incomplete"] = True
                                if "quiescence" in raw:
                                    raw["quiescence"]["complete"] = False
                broker = getattr(slot, "private_broker", None)
                if broker is not None:
                    raw["isolated_broker"] = list(broker.events)
                try:
                    if "admissions" in raw:
                        evaluated = oracle.evaluate(scenario, raw)
                        raw["harness_errors"] = list(evaluated["harness_errors"])
                        evaluated["interleavings"] = sorted(coverage.observed(raw))
                    else:
                        evaluated = {"scenario_id": scenario.scenario_id,
                                     "arm": arm_name, "harness_errors": raw["harness_errors"]}
                except Exception:
                    raw["harness_errors"].append(traceback.format_exc())
                    evaluated = {"scenario_id": scenario.scenario_id, "arm": arm_name,
                                 "harness_errors": raw["harness_errors"]}
            evaluated.update(family=scenario.family, connector=scenario.connector,
                completion_achievable=scenario.completion_achievable,
                expected_operations=len(scenario.operations))
            if control is not None:
                control.record(evaluated)
            with lock:
                for error in raw["harness_errors"]:
                    errors.append((scenario.scenario_id, arm_name, error))
                out["raw"].write(json.dumps(raw, default=str) + "\n")
                out["results"].write(json.dumps(evaluated, default=str) + "\n")
                out["raw"].flush()
                out["results"].flush()
                print(f"{scenario.scenario_id} {arm_name}: "
                      f"errors={len(raw['harness_errors'])} "
                      f"safety_failures={oracle.safety_failures(evaluated)}", flush=True)
            if raw.get("cleanup_incomplete"):
                return  # Do not reuse a slot while an old fault action is alive.


def exit_code(rows, expected_pairs):
    pairs = [(r.get("scenario_id"), r.get("arm")) for r in rows]
    if expected_pairs < 1 or len(pairs) != expected_pairs or len(set(pairs)) != expected_pairs:
        return 1
    for row in rows:
        if row.get("harness_errors") or row.get("cleanup_incomplete"):
            return 1
        if row.get("arm") == "safeact" and oracle.safety_failures(row):
            return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", default="dev")
    parser.add_argument("--arms", default="safeact")
    parser.add_argument("--slots", type=int, default=1)
    parser.add_argument("--families")
    parser.add_argument("--cases", help="comma-separated development scenario ids")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--out", required=True)
    parser.add_argument('--manifest')
    parser.add_argument('--scope', choices=('smoke', 'full', 'calibration', 'diagnostic'))
    parser.add_argument('--calibration')
    parser.add_argument('--diagnostics-from')
    args = parser.parse_args(argv)
    if args.manifest:
        from chaos.frozen import execute
        return execute(args)
    if args.scope or args.calibration or args.diagnostics_from:
        raise SystemExit('--scope/--calibration require a frozen manifest')
    if os.environ.get("SAFEACT_RUN_QUALIFICATION") != "1":
        raise SystemExit("Set SAFEACT_RUN_QUALIFICATION=1 (mock providers only)")
    if not 1 <= args.slots <= len(SLOT_REDIS_DBS):
        raise SystemExit(f"--slots must be 1..{len(SLOT_REDIS_DBS)}")
    scenarios = scenarios_for(args)
    names = [name.strip() for name in args.arms.split(",")]
    arm_classes = arms_named(names)
    from api import logging as api_logging
    api_logging.configure()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=False)
    describe = {"start": environment.describe(), "slots": args.slots,
                "arms": names, "corpus": args.corpus, "scenarios": len(scenarios),
                "scope": "development; qualification not complete"}
    work = queue.Queue()
    for scenario in scenarios:
        work.put(scenario)
    lock, errors = threading.Lock(), []
    workdir = Path(tempfile.mkdtemp(prefix="safeact-qual-"))
    describe["workdir"] = str(workdir)
    started = time.monotonic()
    with (out_dir / "raw.jsonl").open("x") as raw, \
            (out_dir / "results.jsonl").open("x") as results:
        out = {"raw": raw, "results": results}
        threads = [threading.Thread(target=slot_worker, args=(
            index, arm_classes, work, out, lock, workdir, errors))
            for index in range(args.slots)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    describe["end"] = environment.describe()
    describe["seconds"] = round(time.monotonic() - started, 1)
    describe["errors"] = [{"scenario": s, "arm": a, "trace": t[-4000:]}
                          for s, a, t in errors]
    rows = [json.loads(line) for line in (out_dir / "results.jsonl").read_text().splitlines()]
    status = exit_code(rows, len(scenarios) * len(arm_classes))
    describe["exit_code"] = status
    (out_dir / "environment.json").write_text(json.dumps(describe, indent=2))
    seen = {name: {} for name in names}
    for row in rows:
        for name in row.get("interleavings", []):
            arm = seen[row["arm"]]
            arm[name] = arm.get(name, 0) + 1
    required = {arm.name: sorted(critical_labels(arm)) for arm in arm_classes}
    missing = {arm: sorted(set(required[arm]) - set(counts))
               for arm, counts in seen.items()}
    (out_dir / "coverage.json").write_text(json.dumps({
        "declared": coverage.CRITICAL, "applicable": required,
        "not_applicable": {arm: sorted(set(coverage.CRITICAL)-set(labels)) for arm, labels in required.items()},
        "observed": seen, "unobserved": missing,
        "complete": not any(missing.values()), "scope": "development only"}, indent=2))
    try:
        summary = scoring.summarize(rows)
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
        (out_dir / "report.md").write_text(report.render(summary))
        for name, counts in summary["by_arm"].items():
            useful = counts["usefulness"]
            print(f"{name}: safety-failure rows={counts['safety_failure_rows']}; "
                  f"useful completion={useful['completed']}/{useful['achievable']} "
                  "achievable operations", flush=True)
    except Exception:
        describe["errors"].append({"stage": "report", "trace": traceback.format_exc()[-4000:]})
        status = describe["exit_code"] = 1
        (out_dir / "environment.json").write_text(json.dumps(describe, indent=2))
    print(f"done: {len(rows)} rows, {len(errors)} errors, exit={status}; "
          "coverage requires review")
    return status


if __name__ == "__main__":
    sys.exit(main())
