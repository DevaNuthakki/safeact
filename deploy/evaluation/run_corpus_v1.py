"""Development-fixture evaluator for four design-stage SafeAct scenarios.

No database, network, or real provider dispatch.
This is not an independent final holdout.
The committed corpus remains design-stage and NOT_RUN.
"""

import hashlib
import json
from pathlib import Path

from core.attempts import AttemptOutcome
from core.contracts import EvidenceKind
from core.recovery import DecisionKind, decide_recovery

from tests.phase_a.test_attempt_coverage import (
    REJECTED_A,
    REJECTED_B,
    inventory,
    negative_decision,
    observation,
)
from tests.phase_a.test_late_commit_race import DelayedProvider
from tests.phase_a.test_recovery_policy import (
    OPERATION,
    evidence,
    make_request,
)


CORPUS = (
    Path(__file__).resolve().parents[2]
    / "deploy/evaluation/corpus_v1.jsonl"
)

CORPUS_SHA256 = (
    "a8d20f5e8bcac218aafc2efda9878434a0656573097c9c12a8977b25b4f6db0e"
)

RETRY_DECISIONS = frozenset({
    DecisionKind.RETRY_SAME_KEY,
    DecisionKind.RETRY_AFTER_PROOF,
})

EXPECTED_FIELDS = {
    "SA-EVAL-001": frozenset({
        ("policy_decision", "initial_decision"),
        ("policy_decision", "final_decision"),
        ("provider_oracle", "effect_count"),
        ("provider_oracle", "dispatch_count"),
    }),
    "SA-EVAL-002": frozenset({
        ("policy_decision", "initial_decision"),
        ("policy_decision", "retry_allowed"),
    }),
    "SA-EVAL-005": frozenset({
        ("validator_output", "status"),
    }),
    "SA-EVAL-006": frozenset({
        ("policy_decision", "final_decision"),
        ("policy_decision", "retry_allowed"),
    }),
}


def load_corpus():
    raw = CORPUS.read_bytes()

    if hashlib.sha256(raw).hexdigest() != CORPUS_SHA256:
        raise ValueError(
            "Committed development corpus SHA256 mismatch"
        )

    if not raw.endswith(b"\n"):
        raise ValueError("Invalid corpus framing")

    records = [
        json.loads(line)
        for line in raw.decode("utf-8").splitlines()
    ]

    expected_ids = [
        f"SA-EVAL-{index:03d}"
        for index in range(1, 11)
    ]

    if [
        record["scenario_id"]
        for record in records
    ] != expected_ids:
        raise ValueError("Unexpected scenario inventory")

    for record in records:
        if (
            record["partition"] != "design"
            or record["execution_status"] != "NOT_RUN"
            or record["oracle_implementation"] != "UNIMPLEMENTED"
        ):
            raise ValueError(
                "Unexpected corpus lifecycle status"
            )

    return records


def observe_001():
    """In-memory delayed-commit simulation, not a real provider."""
    provider = DelayedProvider()
    dispatch_count = 0
    operation_id = OPERATION.operation_id

    def dispatch():
        nonlocal dispatch_count
        dispatch_count += 1

        try:
            provider.dispatch(operation_id)
        except TimeoutError:
            return

        raise RuntimeError(
            "Expected simulated lost acknowledgment"
        )

    dispatch()

    if (
        provider.pending_count != 1
        or provider.lookup(operation_id) != "NOT_FOUND"
    ):
        raise RuntimeError(
            "Unexpected pending simulated provider state"
        )

    first = decide_recovery(
        make_request(
            evidence=evidence(EvidenceKind.NOT_FOUND)
        )
    )

    # Model the consequences if policy incorrectly permits
    # another external dispatch. This is simulation only.
    if first.kind in RETRY_DECISIONS:
        dispatch()

    provider.complete_pending()

    if provider.lookup(operation_id) != "APPLIED":
        raise RuntimeError(
            "Simulated committed effect not visible"
        )

    final = decide_recovery(
        make_request(
            evidence=evidence(
                EvidenceKind.APPLIED,
                reference="mock-effect-1",
            )
        )
    )

    if final.kind in RETRY_DECISIONS:
        dispatch()
        provider.complete_pending()

    # The simulated provider's effect ledger is independent
    # of SafeAct's policy output.
    return {
        ("policy_decision", "initial_decision"):
            first.kind.name,
        ("policy_decision", "final_decision"):
            final.kind.name,
        ("provider_oracle", "effect_count"):
            provider.ground_truth_effects.count(operation_id),
        ("provider_oracle", "dispatch_count"):
            dispatch_count,
    }


def observe_002():
    decision = negative_decision(
        inventory()
    )

    return {
        ("policy_decision", "initial_decision"):
            decision.kind.name,
        ("policy_decision", "retry_allowed"):
            decision.kind in RETRY_DECISIONS,
    }


def observe_005():
    try:
        observation(
            "attempt-c",
            AttemptOutcome.PROVEN_NOT_APPLIED,
        )
    except ValueError as exc:
        if "terminal observation requires" in str(exc):
            status = "REJECTED"
        else:
            status = "REJECTED_WRONG_REASON"
    else:
        status = "ACCEPTED"

    return {
        ("validator_output", "status"): status,
    }


def observe_006():
    decision = negative_decision(
        inventory(REJECTED_A, REJECTED_B),
        remaining_retries=0,
    )

    return {
        ("policy_decision", "final_decision"):
            decision.kind.name,
        ("policy_decision", "retry_allowed"):
            decision.kind in RETRY_DECISIONS,
    }


ADAPTERS = {
    "SA-EVAL-001": observe_001,
    "SA-EVAL-002": observe_002,
    "SA-EVAL-005": observe_005,
    "SA-EVAL-006": observe_006,
}


def grade(record, observations):
    """Compare observations to fixed corpus expectations."""
    scenario_id = record["scenario_id"]
    signatures = EXPECTED_FIELDS.get(scenario_id)

    if signatures is None:
        return (
            "INCONCLUSIVE",
            "not_an_executable_development_case",
            [],
        )

    expected = {}

    for assertion in record["assertions"]:
        key = (
            assertion["observer"],
            assertion["metric"],
        )

        if key in expected:
            return (
                "INCONCLUSIVE",
                "duplicate_expected_metric",
                [],
            )

        expected[key] = assertion["equals"]

    if set(expected) != signatures:
        return (
            "INCONCLUSIVE",
            "unexpected_expected_metric_contract",
            [],
        )

    if (
        type(observations) is not dict
        or set(observations) != signatures
    ):
        return (
            "INCONCLUSIVE",
            "missing_or_extra_observation",
            [],
        )

    details = []
    mismatched = False

    for observer, metric in sorted(signatures):
        wanted = expected[(observer, metric)]
        got = observations[(observer, metric)]

        matches = (
            type(wanted) is type(got)
            and wanted == got
        )

        if not matches:
            mismatched = True

        details.append({
            "observer": observer,
            "metric": metric,
            "expected": wanted,
            "actual": got,
            "matches": matches,
        })

    if mismatched:
        return (
            "FAIL",
            "observation_mismatch",
            details,
        )

    return (
        "PASS",
        "matched_development_contract",
        details,
    )


def evaluate(records=None, adapters=None):
    if records is None:
        records = load_corpus()

    if adapters is None:
        adapters = ADAPTERS

    results = []

    for record in records:
        scenario_id = record["scenario_id"]

        if scenario_id not in adapters:
            results.append({
                "scenario_id": scenario_id,
                "verdict": "NOT_RUN",
                "reason": "no_development_adapter",
                "observations": [],
            })
            continue

        try:
            observed = adapters[scenario_id]()
            verdict, reason, details = grade(
                record, observed
            )

        except Exception as exc:
            verdict = "INCONCLUSIVE"
            reason = (
                "adapter_exception:"
                + type(exc).__name__
            )
            details = []

        results.append({
            "scenario_id": scenario_id,
            "verdict": verdict,
            "reason": reason,
            "observations": details,
        })

    totals = {
        kind: sum(
            item["verdict"] == kind
            for item in results
        )
        for kind in (
            "PASS",
            "FAIL",
            "INCONCLUSIVE",
            "NOT_RUN",
        )
    }

    return {
        "run_kind": "development_fixture_only",
        "corpus_sha256": CORPUS_SHA256,
        "production_approved": False,
        "final_holdout": False,
        "totals": totals,
        "results": results,
    }


def main():
    report = evaluate()

    print(
        json.dumps(
            report,
            sort_keys=True,
            indent=2,
        )
    )

    if (
        report["totals"]["PASS"] != 4
        or report["totals"]["NOT_RUN"] != 6
        or report["totals"]["FAIL"]
        or report["totals"]["INCONCLUSIVE"]
    ):
        raise SystemExit(
            "STOP: Development evaluator acceptance failed"
        )

    print(
        "PASS: Four development fixture evaluations matched; "
        "six design scenarios remain NOT_RUN"
    )


if __name__ == "__main__":
    main()
