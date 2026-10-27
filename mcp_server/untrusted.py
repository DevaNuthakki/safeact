"""Project an operation's detail view for an agent.

Two kinds of field, kept apart:

- authoritative: values from SafeAct's own vocabulary (states, outcome
  and evidence kinds, provenance, counts, the contract's declared
  capabilities). Anything that does not match that vocabulary is moved
  out of this part.
- untrusted_provider_text: strings that came from a provider or another
  outside source (references, details, observer names, transition
  evidence, unrecognised reasons). Control and bidirectional-format
  characters are removed and each value is truncated. No server
  decision ever reads them; they are returned so an agent can quote
  them, never obey them.
"""

import json
import re
import unicodedata

from mcp_server.tools import UNTRUSTED_NOTE


MAX_TEXT = 200
MAX_ITEMS = 50
WORD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
STATES = frozenset({
    "RECORDED", "CLAIMED", "DISPATCHING", "SUCCEEDED", "FAILED_CONFIRMED",
    "UNKNOWN", "VERIFYING", "RETRY_ELIGIBLE", "ESCALATED_UNRESOLVED",
    "CANCELLED_BEFORE_CLAIM",
})
TERMINAL = frozenset({"SUCCEEDED", "FAILED_CONFIRMED", "CANCELLED_BEFORE_CLAIM"})
UNRESOLVED = frozenset({"UNKNOWN", "VERIFYING", "ESCALATED_UNRESOLVED"})
CONTRACT_FIELDS = (
    "side_effect_class", "native_idempotency", "lookup_semantics",
    "max_attempts",
)

GUIDANCE = {
    "RECORDED": "Accepted and durably recorded; not dispatched yet.",
    "CLAIMED": "A worker holds it; not dispatched yet.",
    "DISPATCHING": "Sent to the provider; the outcome is not known yet.",
    "SUCCEEDED": "The effect is confirmed applied. Do not resubmit.",
    "FAILED_CONFIRMED": (
        "Confirmed not applied. A new submission with a new intent_ref is "
        "allowed if the business still wants it."
    ),
    "UNKNOWN": (
        "The outcome is not known. Do not resubmit: SafeAct will verify "
        "with the provider."
    ),
    "VERIFYING": "SafeAct is verifying with the provider. Do not resubmit.",
    "RETRY_ELIGIBLE": (
        "SafeAct proved a retry safe and will retry under its own key. Do "
        "not resubmit."
    ),
    "ESCALATED_UNRESOLVED": (
        "Still not known after investigation; a human operator is notified. "
        "Do not resubmit. You may request a bounded verification or propose "
        "a resolution for a human to decide."
    ),
    "CANCELLED_BEFORE_CLAIM": "Cancelled before anything was dispatched.",
}


def clean(value):
    """Untrusted text as a short, inert string."""

    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, default=str)
    text = "".join(
        " " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch
        for ch in value
    )
    text = " ".join(text.split())
    return text[:MAX_TEXT] + ("…" if len(text) > MAX_TEXT else "")


def _word(value):
    return isinstance(value, str) and bool(WORD.fullmatch(value))


def status(detail):
    """The compact, authoritative status of one operation."""

    state = detail.get("state")
    if state not in STATES:
        raise ValueError("Unexpected state from SafeAct")
    transitions = detail.get("transitions") or []
    last_reason = transitions[-1].get("reason") if transitions else None
    return {
        "operation_id": detail["operation_id"],
        "action_type": detail["action_type"],
        "resource_id": detail["resource_id"],
        "state": state,
        "seq": detail["seq"],
        "terminal": state in TERMINAL,
        "outcome_known": state not in UNRESOLVED,
        "outcome_class": detail.get("outcome_class"),
        "success_provenance": detail.get("success_provenance"),
        "attempts": len(detail.get("attempts") or []),
        "last_reason": last_reason if _word(last_reason) else None,
        "next_check": _next_check(detail.get("next_check")),
        "guidance": GUIDANCE[state],
    }


def _next_check(found):
    """SafeAct's own next check (0019): kind and times only, no prose."""

    if not isinstance(found, dict) or not _word(found.get("kind")):
        return None
    times = {k: found.get(k) for k in ("not_before", "deadline")}
    return {"kind": found["kind"], **{
        k: v if isinstance(v, str) and len(v) <= 40 else None
        for k, v in times.items()}}


def evidence(detail):
    """Authoritative evidence plus clearly separated untrusted text."""

    untrusted = []

    def outside(field, value):
        if value in (None, "", {}, []):
            return
        if len(untrusted) < MAX_ITEMS:
            untrusted.append({"field": field, "text": clean(value)})

    transitions = []
    for t in (detail.get("transitions") or [])[:MAX_ITEMS]:
        reason = t.get("reason")
        transitions.append({
            "seq": t.get("seq"),
            "from_state": t.get("from_state") if t.get("from_state") in STATES else None,
            "to_state": t.get("to_state") if t.get("to_state") in STATES else None,
            "reason": reason if _word(reason) else None,
            "at": t.get("created_at"),
        })
        if reason is not None and not _word(reason):
            outside(f"transitions[{t.get('seq')}].reason", reason)
        outside(f"transitions[{t.get('seq')}].evidence", t.get("evidence"))

    attempts = []
    for a in (detail.get("attempts") or [])[:MAX_ITEMS]:
        ordinal = a.get("ordinal")
        outcomes = []
        for n, x in enumerate((a.get("outcomes") or [])[:MAX_ITEMS]):
            outcomes.append({
                "outcome": x.get("outcome") if _word(x.get("outcome")) else None,
                "evidence_kind": (
                    x.get("evidence_kind") if _word(x.get("evidence_kind"))
                    else None
                ),
                "provenance": (
                    x.get("provenance") if _word(x.get("provenance")) else None
                ),
                "has_reference": bool(x.get("evidence_reference")),
                "at": x.get("created_at"),
            })
            where = f"attempts[{ordinal}].outcomes[{n}]"
            outside(where + ".evidence_reference", x.get("evidence_reference"))
            outside(where + ".details", x.get("details"))
            outside(where + ".observed_by", x.get("observed_by"))
        attempts.append({"ordinal": ordinal, "outcomes": outcomes})

    contract = detail.get("contract") or {}
    return {
        "authoritative": {
            **status(detail),
            "provider_key_protected": bool(detail.get("provider_key_protected")),
            "contract": {k: contract.get(k) for k in CONTRACT_FIELDS},
            "transitions": transitions,
            "attempt_records": attempts,
        },
        "untrusted_provider_text": untrusted,
        "untrusted_provider_text_note": UNTRUSTED_NOTE,
    }
