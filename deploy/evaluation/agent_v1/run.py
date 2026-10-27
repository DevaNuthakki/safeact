"""Run the agent evaluation (mock providers only).

    # Scripted (free; what CI runs):
    .venv/bin/python -m deploy.evaluation.agent_v1.run --split dev \\
        --model scripted-honest --out /tmp/agent-eval

    # Paid (only after Gate 2; needs the key file and the frozen manifest):
    SAFEACT_ALLOW_PAID_MODEL=1 .venv/bin/python -m deploy.evaluation.agent_v1.run \\
        --split heldout --model openai --manifest deploy/evaluation/agent_v1/manifest_heldout.json

Arms: guarded_agent (the model with the guarded SafeAct MCP tools),
direct_agent (the same model with direct mock-provider tools) and
deterministic (a fixed policy on the guarded tools). Each trial is
scored against the harness ledger and the database; results.jsonl,
report.json, report.md and manifest.json are written to the output
directory as the run goes.

A paid run refuses to start unless its manifest equals the committed
frozen manifest exactly (model snapshot, prices, prompt, tools,
scenarios, scoring, budgets, SDK versions) and its worst case
(trials x per-trial cap) fits in what is left of the total cap in the
spend ledger, so no trial is ever cut for money.
"""

import argparse
import asyncio
import hashlib
import json
import sys
import uuid

from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from api import logging as api_logging
from deploy.evaluation.agent_v1 import report as reporting
from deploy.evaluation.agent_v1 import scenarios, scoring
from deploy.evaluation.agent_v1.harness import (
    DirectProviders, GuardedEnvironment, make_trial,
)
from deploy.evaluation.agent_v1.scenarios import ACTIONS
from mcp_server.agent import runner
from mcp_server.agent.budget import Price, SpendLedger, TrialLimits
from mcp_server.agent.secrets import (
    KeyFileRefused, PaidModelRefused, load_openai_key,
    require_paid_model_allowed,
)
from mcp_server.agent.models import (
    ModelError, OpenAIModel, PolicyModel, TaskSpec, adversarial_policy,
    direct_honest_policy, guarded_policy, observe_model,
)
from mcp_server.direct.server import TOOLS as DIRECT_TOOLS
from mcp_server.direct.server import TOOLSET_VERSION as DIRECT_VERSION
from mcp_server.tools import TOOLS as GUARDED_TOOLS
from mcp_server.tools import TOOLSET_VERSION as GUARDED_VERSION
from mcp_server.tools import toolset_digest


ROOT = Path(__file__).resolve().parent
SPEND_LEDGER = ROOT / "spend.jsonl"
TOTAL_CAP_USD = 4.00
ARMS = ("guarded_agent", "direct_agent", "deterministic")

# Changes made after looking at paid results, disclosed in every report.
DISCLOSURES = (
    "Harness defect found in the paid dev calibration (2026-10-20) and "
    "fixed before any held-out run: get_action_status in "
    "safeact-guarded-v1 woke on every internal UNKNOWN <-> VERIFYING flip, "
    "which used up the deterministic caller's 12 turns on AG1-D04. "
    "safeact-guarded-v2 wakes only when the outcome changes (UNKNOWN and "
    "VERIFYING count as the same). The v1 calibration results are kept "
    "unedited in docs/phase-d/agent-eval-v1/dev-calibration/.",
    "Scoring rule changed after the paid dev calibration and before any "
    "held-out result existed (agent-eval-scoring-v2): in every scenario "
    "whose expected outcome is escalation, either unresolved or "
    "needs_human_review counts as correct, for all arms alike; neither "
    "ever counts as success. The dev calibration is shown under both rules "
    "in docs/phase-d/agent-eval-v1/dev-calibration/rescored.md.",
    "Ground-truth attribution fixed after the v2 dev calibration and "
    "before any held-out result existed: effects were attributed to a "
    "resource through the provider correlation id, so a direct agent that "
    "reused its refund key as an email message id had the email counted "
    "against the charge (AG1-D05, direct arm: recorded 3 effects on the "
    "charge, truly 2 plus 1 email to another customer). Effects are now "
    "counted when each provider applies them, from that request's own "
    "parameters, and effects on any other resource are reported as "
    "orphan effects (blueprint section 09), a column of their own in the "
    "headline results for every arm (not part of the correctness or "
    "unsafe counts). The v2 calibration results are kept unedited, with "
    "the correction noted in docs/phase-d/agent-eval-v1/dev-calibration-v2/"
    "CORRECTIONS.md.",
)

# Lifecycle of pinned model snapshots, from OpenAI's deprecations page
# (verified 2026-10-20). Part of the frozen manifest; each paid run also
# records the snapshot's shutdown_date as the List models endpoint
# reports it on the day.
MODEL_LIFECYCLE = {
    "gpt-4.1-mini-2025-04-14": {
        "deprecated_on": "2026-10-14",
        "shutdown_date": "2027-01-14",
        "replacement": "gpt-6-luna",
        "source": "https://developers.openai.com/api/docs/deprecations "
                  "(2026-10-14: GPT-4 series model deprecations)",
        "note": "Results cannot be rerun on this snapshot after "
                "2027-01-14. The harness is model-agnostic; the run can be "
                "repeated on the replacement with a new frozen manifest.",
    },
}
MODEL_ARMS = ("guarded_agent", "direct_agent")
MODELS = ("scripted-honest", "scripted-adversarial", "openai")


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_manifest(args, limits, price):
    return {
        "manifest_version": 1,
        "run_kind": "paid" if args.model == "openai" else "scripted",
        "split": args.split,
        "scenario_version": scenarios.SCENARIO_VERSION,
        "scenario_sha256": scenarios.digest(args.split),
        "scenarios": args.scenario or "all",
        "arms": list(args.arms),
        "trials_per_scenario": args.trials,
        "model": args.snapshot if args.model == "openai" else args.model,
        "model_lifecycle": (
            MODEL_LIFECYCLE.get(args.snapshot) if args.model == "openai" else None
        ),
        "temperature": args.temperature,
        "price_per_million_usd": asdict(price) if price else None,
        "prompt_version": runner.PROMPT_VERSION,
        "prompt_sha256": _sha256(runner.PROMPT_PATH),
        "guarded_toolset": GUARDED_VERSION,
        "guarded_toolset_sha256": toolset_digest(GUARDED_TOOLS),
        "direct_toolset": DIRECT_VERSION,
        "direct_toolset_sha256": toolset_digest(DIRECT_TOOLS),
        "scoring_version": scoring.SCORING_VERSION,
        "disclosures": list(DISCLOSURES),
        "limits": asdict(limits),
        "total_cap_usd": TOTAL_CAP_USD,
        "sdk": {"mcp": version("mcp"), "openai": version("openai")},
    }


def task_spec(trial):
    _, account, _, _ = ACTIONS[trial.scenario["action"]]
    return TaskSpec(
        action=trial.scenario["action"], account=account,
        payload=dict(trial.payload), intent_ref=trial.intent_ref,
        prior_ref=trial.prior_ref,
        prior_operation_id=trial.extra.get("prior_operation_id"),
    )


def model_for(arm, trial, args):
    spec = task_spec(trial)
    if arm == "deterministic":
        return PolicyModel(guarded_policy(spec))
    if args.model == "openai":
        return OpenAIModel(args.snapshot, temperature=args.temperature)
    if args.model == "scripted-adversarial":
        return PolicyModel(adversarial_policy(
            spec, "direct" if arm == "direct_agent" else "guarded"))
    if arm == "direct_agent":
        return PolicyModel(direct_honest_policy(spec))
    return PolicyModel(guarded_policy(spec))


def preflight(args, limits, ledger, selected):
    model_trials = sum(1 for _ in selected) * args.trials * len(
        [a for a in args.arms if a in MODEL_ARMS])
    worst = model_trials * limits.trial_usd
    if args.model == "openai":
        if not args.manifest:
            raise SystemExit("A paid run needs --manifest (the frozen manifest)")
        # Before anything starts: the paid gate (never in CI) and a safe
        # key file. Neither check ever shows the key.
        try:
            require_paid_model_allowed()
            load_openai_key()
        except (PaidModelRefused, KeyFileRefused) as refused:
            raise SystemExit(f"Refused: {refused}") from None
        if worst > ledger.remaining() + 1e-9:
            raise SystemExit(
                f"Refused: worst case ${worst:.2f} for {model_trials} model "
                f"trials exceeds the ${ledger.remaining():.2f} left of the "
                f"${ledger.total_cap:.2f} total cap"
            )
    return model_trials, worst


def run(args):
    limits = TrialLimits(
        max_turns=args.max_turns, max_tool_calls=args.max_tool_calls,
        max_output_tokens=args.max_output_tokens, trial_usd=args.trial_usd,
    )
    price = None
    if args.model == "openai":
        price = Price(args.price_input, args.price_cached, args.price_output)
    manifest = build_manifest(args, limits, price)
    if args.manifest:
        frozen = json.loads(Path(args.manifest).read_text("utf-8"))
        if frozen != manifest:
            different = sorted(k for k in set(frozen) | set(manifest)
                               if frozen.get(k) != manifest.get(k))
            raise SystemExit(f"Refused: manifest differs in {different}")

    selected = [s for s in scenarios.load(args.split)
                if not args.scenario or s["scenario_id"] in args.scenario]
    ledger = SpendLedger(args.spend_ledger, TOTAL_CAP_USD) if price else None
    model_trials, worst = preflight(args, limits, ledger, selected)

    run_id = args.run_id or datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest_out = {**manifest, "run_id": run_id,
                    "worst_case_usd": round(worst, 4),
                    "model_trials": model_trials,
                    # Run-specific, not part of the frozen manifest: which
                    # trial indices this run executes (0-based). Seeded
                    # targets depend on the index, so an extension run's
                    # trials are new trials, never repeats.
                    "trial_indices": list(range(
                        args.first_trial, args.first_trial + args.trials))}
    if args.model == "openai":
        # The snapshot as the List models endpoint reports it today.
        try:
            observed = observe_model(args.snapshot)
        except ModelError as error:
            raise SystemExit(f"Refused: {error}") from None
        declared = (manifest.get("model_lifecycle") or {}).get("shutdown_date")
        observed["matches_declared_shutdown_date"] = (
            observed["shutdown_date"] == declared
        )
        manifest_out["model_observed"] = observed
        print(f"model {observed['id']}: shutdown_date="
              f"{observed['shutdown_date']} (declared {declared})", flush=True)
    (out / "manifest.json").write_text(json.dumps(manifest_out, indent=2,
                                                  sort_keys=True) + "\n")
    print(f"run {run_id}: {len(selected)} scenarios x {args.trials} trial(s) x "
          f"arms {list(args.arms)}; worst case ${worst:.2f}", flush=True)

    guarded = None
    if any(a in ("guarded_agent", "deterministic") for a in args.arms):
        guarded = GuardedEnvironment().start()
    direct = DirectProviders()
    rows = []
    try:
        with (out / "results.jsonl").open("w", encoding="utf-8") as results:
            for scenario in selected:
                for arm in args.arms:
                    for index in range(args.first_trial,
                                       args.first_trial + args.trials):
                        env = direct if arm == "direct_agent" else guarded
                        trial = env.prepare(make_trial(arm, scenario, index, run_id))
                        model = model_for(arm, trial, args)
                        record = asyncio.run(runner.run_trial(
                            trial, model, limits,
                            mcp_url=guarded.mcp_url if guarded else None,
                            price=price if arm in MODEL_ARMS else None,
                            ledger=ledger if arm in MODEL_ARMS else None,
                            run_id=run_id,
                        ))
                        env.settle(trial)
                        record["effects_on_target"] = env.effects(trial)
                        record["orphan_effects"] = env.orphan_effects(trial)
                        record["provider_calls_on_target"] = env.provider_calls(trial)
                        record["safeact_states"] = env.states(trial.target)
                        record["safeact_reasons"] = env.reasons(trial.target)
                        record["score"] = scoring.score(record, scenario)
                        rows.append(record)
                        results.write(json.dumps(record, sort_keys=True, default=str) + "\n")
                        results.flush()
                        s = record["score"]
                        print(f"  {scenario['scenario_id']} {arm} #{index}: "
                              f"report={s['report']} effects={s['effects_on_target']} "
                              f"correct={s['correct_business_outcome']} "
                              f"unsafe={s['unsafe_attempted']}/{s['unsafe_executed']} "
                              f"cost=${s['cost_usd']:.4f}", flush=True)
    finally:
        if guarded is not None:
            guarded.stop()

    report = reporting.build(rows, manifest_out)
    (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True,
                                                default=str) + "\n")
    (out / "report.md").write_text(reporting.markdown(report, note=args.note))
    return report


def parse(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", choices=sorted(scenarios.SPLITS), required=True)
    parser.add_argument("--arms", type=lambda v: tuple(v.split(",")),
                        default=ARMS)
    parser.add_argument("--scenario", action="append",
                        help="limit to these scenario ids (repeatable)")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--first-trial", type=int, default=0,
                        help="0-based index of the first trial (extension runs)")
    parser.add_argument("--model", choices=MODELS, required=True)
    parser.add_argument("--snapshot", help="pinned model snapshot (openai)")
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--price-input", type=float, default=0.0)
    parser.add_argument("--price-cached", type=float, default=0.0)
    parser.add_argument("--price-output", type=float, default=0.0)
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--max-tool-calls", type=int, default=20)
    parser.add_argument("--max-output-tokens", type=int, default=800)
    parser.add_argument("--trial-usd", type=float, default=0.10)
    parser.add_argument("--spend-ledger", default=str(SPEND_LEDGER))
    parser.add_argument("--manifest", help="frozen manifest to match exactly")
    parser.add_argument("--run-id")
    parser.add_argument("--note", help="a line to put at the top of report.md")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if any(a not in ARMS for a in args.arms):
        parser.error(f"arms must be among {ARMS}")
    if args.model == "openai" and not args.snapshot:
        parser.error("--snapshot is required for --model openai")
    return args


def main(argv=None):
    # SafeAct's redacted JSON logs (MCP tool calls, stack loops, any
    # warning) go to stderr, which every run saves unfiltered.
    api_logging.configure()
    run(parse(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
