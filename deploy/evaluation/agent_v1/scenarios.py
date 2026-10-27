"""Versioned agent-evaluation scenarios: schema, loader and digests.

Two splits, each a JSONL file pinned by SHA-256
(tests/phase_a/test_agent_eval_contract.py):

- dev: one scenario per family. Prompt and harness development and the
  paid calibration run use only these.
- heldout: unseen combinations of family, connector, timing and wording.
  Never used to tune prompts; run only after the manifest is frozen.

A scenario names a business task, the provider fault its resource
exhibits, whether the caller is authorized for it, an optional prior
attempt the harness makes before the trial, and the ground truth a
correct outcome must match. Resource ids, intent references and keys
are generated per trial (seeded), so trials never share state.
"""

import hashlib
import json

from pathlib import Path

from deploy.evaluation.agent_v1.faults import KINDS


ROOT = Path(__file__).resolve().parent
SPLITS = {"dev": ROOT / "scenarios_dev.jsonl",
          "heldout": ROOT / "scenarios_heldout.jsonl"}
SCENARIO_VERSION = "agent-eval-v1"

FAMILIES = (
    "no_fault_control",
    "timeout_after_success",
    "pending_provider_work",
    "stale_lookup",
    "key_expiry",
    "denied_access",
    "malicious_tool_text",
    "attempted_self_approval",
)
ACTIONS = {
    "refund_payment": ("mock-payments", "mock-payments-account", "charge_id", "ch"),
    "create_replacement_order": ("mock-crm", "mock-crm-account", "case_id", "case"),
    "send_refund_confirmation": ("mock-email", "mock-email-account", "customer_id", "cust"),
}
# What a caller may report at the end of a trial (the report_outcome tool).
REPORTS = (
    "succeeded", "failed", "pending", "unresolved", "needs_human_review",
    "refused",
)
FIELDS = {
    "scenario_id", "split", "family", "action", "contract_version", "fault",
    "granted", "setup", "payload", "task", "expected", "seed",
}
EXPECTED_FIELDS = {"effects", "reports", "escalation_justified",
                   "approval_required"}


class ScenarioError(ValueError):
    pass


def _check(record, split):
    if set(record) != FIELDS:
        raise ScenarioError(f"{record.get('scenario_id')}: fields {sorted(set(record) ^ FIELDS)}")
    sid = record["scenario_id"]
    if record["split"] != split:
        raise ScenarioError(f"{sid}: wrong split")
    if record["family"] not in FAMILIES:
        raise ScenarioError(f"{sid}: unknown family")
    if record["action"] not in ACTIONS:
        raise ScenarioError(f"{sid}: unknown action")
    if record["fault"].get("kind") not in KINDS:
        raise ScenarioError(f"{sid}: unknown fault")
    if record["setup"] not in ("none", "prior_attempt"):
        raise ScenarioError(f"{sid}: unknown setup")
    expected = record["expected"]
    if set(expected) != EXPECTED_FIELDS:
        raise ScenarioError(f"{sid}: expected fields")
    if not expected["reports"] or not set(expected["reports"]) <= set(REPORTS):
        raise ScenarioError(f"{sid}: unknown report")
    if record["granted"] is False and expected["effects"] != 0:
        raise ScenarioError(f"{sid}: an ungranted target must have no effect")
    for placeholder in ("{resource}", "{intent_ref}"):
        if placeholder not in record["task"]:
            raise ScenarioError(f"{sid}: task lacks {placeholder}")
    return record


def load(split):
    """The scenarios of one split, in file order, schema-checked."""

    records = []
    for line in SPLITS[split].read_text("utf-8").splitlines():
        if line.strip():
            records.append(_check(json.loads(line), split))
    ids = [r["scenario_id"] for r in records]
    if len(ids) != len(set(ids)):
        raise ScenarioError("duplicate scenario ids")
    return records


def digest(split):
    return hashlib.sha256(SPLITS[split].read_bytes()).hexdigest()
