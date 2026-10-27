"""Seeded qualification scenarios across F01-F16 (blueprint §08, §09).

A scenario is data: which connector and contract, which authorized
operations (each with its own resource id, so the independent ledger can
attribute every provider effect without trusting any arm), and a fault
schedule. Generation is a pure function of (split, family, index, seed):
the same seed always gives the same scenario, so a run can be replayed;
the actual interleaving is still recorded per run, because a seed does
not fix operating-system scheduling (§09).

Fault kinds (applied by chaos.runner):
  provider     set a mock switch before submitting (one-shot switches fire
               on the next request): {provider, switch, value}
  later        set a mock switch after at_ms: {provider, switch, value, at_ms}
  hold         the next request is accepted but commits only after
               release_ms after acceptance (late commit):
               {provider, release_ms, respond_now}
  toxic        a Toxiproxy toxic from at_ms for duration_ms:
               {provider, toxic, attributes, stream, at_ms, duration_ms}
  down         the proxy refuses connections from at_ms for duration_ms
  failpoint    arm a deterministic failpoint: {point, action}
  broker_loss  stop the consumer before admission, observe a queued message,
               flush the private broker DB, restart the consumer (F08)
  broker_redelivery  hold an accepted request and restore its real unacked
               message through the Redis transport before the task returns
  broker_kill  kill and restart a separately owned Redis server without
               persistence while admitted, unclaimed work is queued
  outage       provider outage from at_ms for duration_ms (F16)
  rate_limit   the next `count` requests answer 429: {provider, count,
               retry_after}
  reveal       hidden/unindexed effects become visible at at_ms (F07)
  hold_after_escalation  hold an accepted request until an independent
               committed database sample observes escalation (F07)
  hold_after_empty_lookup  preserve the seeded minimum delay, but wait for
               the actual empty HTTP lookup before releasing a commit (F07)

Every scenario also states whether a correct system *can* complete the
work automatically (`completion_achievable`): safety versus usefulness
(§09) is scored against it, so escalating everything never looks equal
to completing work safely.
"""

import hashlib
import json
import random

from dataclasses import asdict, dataclass, field


FAMILIES = (
    "CONTROL", "F01", "F02", "F03", "F04", "F05", "F06", "F07", "F08",
    "F09", "F10", "F11", "F12", "F13", "F14", "F15", "F16",
)
# Adverse combinations, held out of development entirely (M5.11).
COMBINATIONS = (
    "F06+F07", "F05+F08", "F16+F11", "F09+F03", "F13+F12", "F04x8",
)

CONNECTORS = {
    "payments": ("mock-payments", "refund_payment"),
    "crm": ("mock-crm", "create_replacement_order"),
    "email": ("mock-email", "send_refund_confirmation"),
}


@dataclass(frozen=True)
class OperationSpec:
    resource_id: str
    amount_minor: int
    submits: int = 1             # identical submissions (F04)
    concurrent: bool = False     # submitted from parallel callers
    changed_resubmit: bool = False  # a second submission, changed (F10)
    acceptable_terminal: tuple = ("confirmed_success",)


@dataclass(frozen=True)
class WorkflowSpec:
    customer_id: str
    case_id: str
    sku: str
    charge_contract: str = "q-v2"
    order_contract: str = "q-v2"
    compensation_contract: str = "q-v2"


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    split: str
    family: str
    connector: str
    contract_version: str
    seed: int
    operations: tuple
    faults: tuple
    completion_achievable: bool
    settle_seconds: float
    notes: str = ""
    tags: tuple = field(default_factory=tuple)
    workflow: WorkflowSpec | None = None
    review_mode: str | None = None

    def to_json(self):
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def from_json(text):
        data = json.loads(text)
        data["operations"] = tuple(OperationSpec(**dict(
            o, acceptable_terminal=tuple(o.get("acceptable_terminal", (
                "confirmed_success",))))) for o in data["operations"])
        data["faults"] = tuple(tuple(sorted(f.items())) if isinstance(f, dict)
                               else f for f in data["faults"])
        data["faults"] = tuple(dict(f) for f in data["faults"])
        data["tags"] = tuple(data.get("tags", ()))
        if data.get("workflow") is not None:
            data["workflow"] = WorkflowSpec(**data["workflow"])
        return Scenario(**data)


def _resource(connector, scenario_id, index):
    stem = hashlib.sha256(f"{scenario_id}:{index}".encode()).hexdigest()[:12]
    prefix = {"payments": "ch", "crm": "case", "email": "cust"}[connector]
    return f"{prefix}_q_{stem}"


def _op(rng, connector, scenario_id, index=0, **changes):
    return OperationSpec(_resource(connector, scenario_id, index),
                         rng.randrange(100, 5000), **changes)


# -- family templates ------------------------------------------------------
# Each returns (connector, contract, operations, faults, achievable, notes).

def _control(rng, sid):
    connector = rng.choice(("payments", "crm", "email"))
    return connector, "q-v2", [_op(rng, connector, sid)], [], True, "no fault"


def _f01(rng, sid):
    connector = rng.choice(("payments", "payments", "crm", "email"))
    network = rng.random() < 0.4
    faults = ([{"kind": "toxic", "provider": connector, "toxic": "timeout",
                "attributes": {"timeout": 0}, "stream": "downstream",
                "at_ms": 0, "duration_ms": 1500}]
              if network else
              [{"kind": "provider", "provider": connector,
                "switch": "fail_after_effect", "value": True}])
    # Payments can verify by lookup; CRM may (if indexed); email cannot.
    achievable = connector != "email"
    return (connector, "q-v2", [_op(rng, connector, sid)], faults, achievable,
            "effect applied, response lost (network)" if network
            else "effect applied, response lost (provider)")


def _f02(rng, sid):
    connector = rng.choice(("payments", "crm", "email"))
    retryable = connector != "email" and rng.random() < 0.6
    faults = [{"kind": "provider", "provider": connector,
               "switch": "reject_next_retryable" if retryable else "reject_next",
               "value": f"rejected-{rng.randrange(10**6)}"}]
    expected = ("confirmed_success",) if retryable else ("confirmed_failure",)
    return (connector, "q-v2", [_op(rng, connector, sid,
            acceptable_terminal=expected)], faults, True,
            "rejected before execution, retryable" if retryable
            else "rejected before execution, permanent")


def _f03(rng, sid):
    connector = rng.choice(("payments", "crm", "email"))
    faults = [{"kind": "provider", "provider": connector,
               "switch": "server_error_after_effect", "value": True}]
    return (connector, "q-v2", [_op(rng, connector, sid)], faults,
            connector != "email", "5xx after commit")


def _f04(rng, sid, *, copies=None):
    connector = rng.choice(("payments", "crm", "email"))
    copies = copies or rng.randrange(2, 6)
    op = _op(rng, connector, sid, submits=copies, concurrent=rng.random() < 0.7)
    faults = []
    if rng.random() < 0.3:
        rng.randrange(0, 300)  # Preserve the remaining seeded choices.
        faults.append({"kind": "broker_loss"})
    return (connector, "q-v2", [op], faults, True,
            f"{copies} identical submissions")


F05_POINTS = ("after_provider_call", "after_fence_commit", "before_outcome_commit")


def _f05(rng, sid, connector=None, point=None):
    connector = connector or rng.choice(("payments", "crm"))
    point = point or rng.choice(F05_POINTS)
    faults = [{"kind": "worker_kill", "point": point}]
    # Past the dispatch-risk fence nothing proves the request was not sent:
    # only a native key makes completion possible. CRM's lookup can find
    # a sent record; email has no lookup, so it must never be redispatched.
    achievable = connector == "payments" or (
        connector == "crm" and point != "after_fence_commit")
    expected = ("confirmed_success",) if achievable else ("escalated_unresolved",)
    return (connector, "q-v2", [_op(rng, connector, sid, acceptable_terminal=expected)],
            faults, achievable, f"worker dies at {point}")


def _f06(rng, sid, connector=None):
    # The held request answers before its 6 s deadline, so the provider's
    # own response can complete it; the lease expiring is no reason to
    # redispatch (email: never, it has neither key nor lookup).
    connector = connector or rng.choice(("payments", "crm"))
    faults = [{"kind": "hold", "provider": connector, "respond_now": False,
               "release_ms": rng.choice((4000, 4500, 5000))}]
    return (connector, "q-v2-inflight", [_op(rng, connector, sid)], faults,
            True, "accepted provider request runs beyond the persisted worker lease")


def _f07(rng, sid):
    connector = rng.choice(("payments", "crm"))
    contract = "q-v2-eventual" if connector == "payments" else "q-v2"
    faults = [{"kind": "hold_after_empty_lookup", "provider": connector, "respond_now": True,
               "release_ms": rng.randrange(1500, 5000)}]
    if connector == "payments":
        faults.append({"kind": "provider", "provider": connector,
                       "switch": "hide_new_effects", "value": True})
    else:
        faults.append({"kind": "provider", "provider": connector,
                       "switch": "index_lag", "value": True})
    faults.append({"kind": "reveal", "provider": connector,
                   "at_ms": rng.randrange(5000, 12000)})
    return (connector, contract, [_op(rng, connector, sid)], faults,
            True,
            "lookup empty before a late commit")


def _f08(rng, sid):
    connector = rng.choice(("payments", "crm", "email"))
    rng.randrange(0, 200)  # Preserve the existing worker-crash choice.
    faults = [{"kind": "broker_loss"}]
    if rng.random() < 0.5:
        faults.append({"kind": "worker_kill", "point": "after_claim_commit"})
    return (connector, "q-v2", [_op(rng, connector, sid)], faults, True,
            "queued messages lost; consumer restarted; optional observed kill after claim")


def _f07_after_escalation(rng, sid, connector):
    return (connector, "q-v2", [_op(rng, connector, sid)],
            [{"kind": "hold_after_escalation", "provider": connector}], True,
            "accepted request commits only after independently observed escalation")


def _f08_redelivery(rng, sid, connector):
    return (connector, "q-v2-inflight", [_op(rng, connector, sid)],
            [{"kind": "broker_redelivery", "provider": connector}], True,
            "early Redis transport restoration while the original request is accepted and held")


def _f08_kill(rng, sid, connector):
    return (connector, "q-v2", [_op(rng, connector, sid)],
            [{"kind": "broker_kill"}], True,
            "queued work lost after SIGKILL and restart of a private Redis server without persistence")


def _f09(rng, sid):
    # The first request's outcome is lost; the provider then stays down
    # past the key's 6 s retention (protected only until 6 - 2 = 4 s), and
    # no lookup answers until after it. A retry after that would be
    # unprotected: a second refund if the first one applied.
    faults = [{"kind": "provider", "provider": "payments",
               "switch": rng.choice(("fail_after_effect", "fail_before_effect")),
               "value": True},
              {"kind": "provider", "provider": "payments",
               "switch": "lookup_unavailable", "value": True},
              {"kind": "outage", "provider": "payments", "after_call": 1,
               "at_ms": 50, "duration_ms": rng.randrange(6500, 8000)},
              {"kind": "later", "provider": "payments", "after_call": 1,
               "switch": "lookup_unavailable", "value": False,
               "at_ms": rng.randrange(8500, 10000)}]
    return ("payments", "q-v2-short", [_op(rng, "payments", sid,
            acceptable_terminal=("escalated_unresolved",))], faults,
            False, "idempotency retention expires before the outcome is known")


def _f10(rng, sid):
    connector = rng.choice(("payments", "crm", "email"))
    return (connector, "q-v2",
            [_op(rng, connector, sid, changed_resubmit=True)], [], True,
            "same request key, changed payload")


def _f11(rng, sid):
    connector = rng.choice(("payments", "crm", "email"))
    point = rng.choice(("after_claim_commit", "after_fence_commit",
                        "after_outcome_commit"))
    faults = [{"kind": "failpoint", "point": point, "action": "lost_ack"}]
    achievable = connector == "payments" or point != "after_fence_commit"
    expected = ("confirmed_success",) if achievable else ("escalated_unresolved",)
    return (connector, "q-v2", [_op(rng, connector, sid, acceptable_terminal=expected)], faults, achievable,
            f"commit acknowledgement lost at {point}")


def _f12(rng, sid):
    connector = rng.choice(("payments", "crm"))
    faults = [{"kind": "provider", "provider": connector,
               "switch": "fail_after_effect", "value": True}]
    if connector == "crm":
        faults.append({"kind": "provider", "provider": "crm",
                       "switch": "corrupt_search", "value": True})
    else:
        faults.append({"kind": "provider", "provider": "payments",
                       "switch": "lookup_unavailable", "value": True})
    return (connector, "q-v2", [_op(rng, connector, sid)], faults,
            connector == "payments",
            "lookup fails or lies outside its contract")


def _f13(rng, sid, variant=None):
    modes = ("lost_response", "hidden_response", "rejected", "order_unknown", "control")
    mode = modes[rng.randrange(len(modes)) if variant is None else variant]
    achievable = mode in ("lost_response", "control")
    terminal = "confirmed_success" if mode == "control" else (
        "verified_compensation" if mode == "lost_response" else "escalated_unresolved")
    op = OperationSpec(_resource("email", sid, 0), rng.randrange(100, 5000),
                       acceptable_terminal=(terminal,))
    return ("payments", "q-v2", [op], [{"kind": "workflow", "mode": mode}],
            achievable, f"fixed charge/order workflow: {mode}")


def _f14(rng, sid, variant=None):
    mode = ("concurrent", "stale", "protected_retry")[
        rng.randrange(3) if variant is None else variant]
    faults = ([{"kind": "provider", "provider": "crm",
                "switch": "reject_next_retryable", "value": "qualification-rejection"}]
              if mode == "protected_retry" else [
                  {"kind": "provider", "provider": "crm", "switch": "fail_after_effect", "value": True},
                  {"kind": "provider", "provider": "crm", "switch": "index_lag", "value": True}])
    return ("crm", "q-v2", [_op(rng, "crm", sid)], faults,
            True, f"human review: {mode}")


def _f15(rng, sid):
    op = _op(rng, "crm", sid, submits=4, concurrent=True)
    faults = [{"kind": "provider", "provider": "crm", "switch": "fail_after_effect", "value": True},
              {"kind": "provider", "provider": "crm", "switch": "index_lag", "value": True}]
    return ("crm", "q-v2", [op], faults, True,
            "credential rotation, conflicting intent and attempted self-approval; real lookup recovery")


def _f16(rng, sid, connector=None, storm=None):
    connector = connector or rng.choice(("payments", "crm"))
    count = rng.randrange(3, 9)
    ops = [_op(rng, connector, sid, i) for i in range(count)]
    if rng.random() < 0.5 if storm is None else storm:
        faults = [{"kind": "rate_limit", "provider": connector,
                   "count": rng.randrange(count, count * 3),
                   "retry_after": rng.choice((None, 0.5, 1.0))}]
        notes = "429 storm"
    else:
        faults = [{"kind": "outage", "provider": connector, "at_ms": 0,
                   "duration_ms": rng.randrange(3000, 12000)}]
        notes = "long outage"
    return (connector, "q-v2", ops, faults,
            _storm_achievable(connector, len(ops), faults), notes)


def _storm_achievable(connector, operation_count, faults):
    """Can all requested work complete within the shared attempt/window bounds?

    This is a contract declaration, independent of an arm's measured outcome.
    A global rejection budget needs enough spare attempts across the workload.
    Ambiguous CRM outages have no native retry protection. An outage still
    active at the investigation deadline cannot be resolved by conserving keys.
    """
    from chaos.profiles import profile
    contract = profile(CONNECTORS[connector][0], 'q-v2')
    for fault in faults:
        if fault['kind'] == 'rate_limit':
            # CRM's 429 is ambiguous under its connector contract; the mock's
            # private knowledge that no write happened is not retry evidence.
            if connector != 'payments' or fault['count'] > operation_count * (contract.max_attempts - 1):
                return False
        elif fault['kind'] == 'outage':
            if (connector != 'payments' or
                    fault['duration_ms'] >= contract.investigation_window.total_seconds()*1000):
                return False
    return True


# Email cells (A3 for an irreversible connector without lookup): every k-th
# index from k on, so each index keeps its own seeded content and the
# reviewed development cases (indices below k) are unchanged.
EMAIL_SLOTS = {"F05": 5, "F06": 4, "F16": 5}


def _email_slot(family, index):
    k = EMAIL_SLOTS.get(family)
    return k is not None and index > 0 and index % k == 0


def _email_cell(family, index, rng, sid):
    """Spread the email variants evenly; the first is the A3-critical one.

    F05 cycles its kill points from after_provider_call (sent, outcome lost);
    F16 alternates 429 storm (ambiguous, starting there) and outage.
    """
    n = index // EMAIL_SLOTS[family] - 1
    if family == "F05":
        return _f05(rng, sid, "email", F05_POINTS[n % 3])
    if family == "F16":
        return _f16(rng, sid, "email", n % 2 == 0)
    return _f06(rng, sid, "email")


def _f16_deadline(rng, sid, connector):
    # Recovery at 12 seconds is outside the shared 8-second investigation
    # window. A native key cannot extend that deadline or the attempt budget.
    expected = ("escalated_unresolved",)
    return (connector, "q-v2", [_op(rng, connector, sid, acceptable_terminal=expected)],
            [{"kind": "outage", "provider": connector, "at_ms": 0, "duration_ms": 12000}],
            False, "continuous provider outage crosses the investigation deadline")


TEMPLATES = {
    "CONTROL": _control, "F01": _f01, "F02": _f02, "F03": _f03, "F04": _f04,
    "F05": _f05, "F06": _f06, "F07": _f07, "F08": _f08, "F09": _f09,
    "F10": _f10, "F11": _f11, "F12": _f12, "F13": _f13, "F14": _f14,
    "F15": _f15, "F16": _f16,
}


def _combined(rng, sid, family):
    if family == "F04x8":
        return _f04(rng, sid, copies=8)
    if family == 'F13+F12':
        # Only the inverse lookup is unavailable. Preserve a completed charge
        # and confirmed order rejection; never retarget a CRM search switch.
        mode = rng.choice((0, 1))
        connector, version, ops, faults, achievable, notes = _f13(rng, sid, mode)
        faults.append(dict(kind='workflow_lookup', provider='payments',
            step='compensation', switch='lookup_unavailable', value=True))
        return connector, version, ops, faults, achievable, notes+'; inverse lookup unavailable'
    if family == 'F06+F07':
        connector = rng.choice(('payments', 'crm'))
        version = 'q-v2-eventual-inflight' if connector == 'payments' else 'q-v2-inflight'
        faults = [dict(kind='hold_after_empty_lookup', provider=connector,
            respond_now=False, release_ms=rng.choice((4500, 5000))),
            dict(kind='provider', provider=connector,
                switch='hide_new_effects' if connector == 'payments' else 'index_lag', value=True),
            dict(kind='reveal', provider=connector, at_ms=9000)]
        return (connector, version, [_op(rng, connector, sid)], faults, True,
                'same accepted request crosses its lease and commits after an actual empty lookup')
    first, second = family.split("+")
    c1, v1, ops, f1, a1, n1 = TEMPLATES[first](rng, sid)
    _, _, _, f2, a2, n2 = TEMPLATES[second](random.Random(rng.random()), sid)
    # The second family's faults on the first's connector and operation.
    moved = []
    for fault in f2:
        fault = dict(fault)
        if family == 'F05+F08' and fault['kind'] == 'worker_kill':
            continue  # One owned parked-process watcher at the selected F05 boundary.
        if "provider" in fault and fault["provider"] not in (c1,):
            fault["provider"] = c1
        moved.append(fault)
    if family == 'F16+F11':
        a2 = c1 == 'payments' or all(f.get('point') != 'after_fence_commit' for f in moved)
    return c1, v1, ops, list(f1) + moved, a1 and a2, f"{n1}; {n2}"


SETTLE = {"F05": 30.0, "F07": 25.0, "F08": 30.0, "F09": 25.0, "F16": 45.0, "F13": 25.0, "F14": 35.0, "F15": 40.0}


def generate(split, family, index, seed):
    if split == "dev" and family in COMBINATIONS:
        raise ValueError("Held-out combinations cannot be used for development")
    if family not in TEMPLATES and family not in COMBINATIONS:
        raise ValueError(f"Unknown scenario family: {family}")
    rng = random.Random(f"{split}:{family}:{index}:{seed}")
    sid = f"{split[0].upper()}-{family}-{index:04d}"
    # Fixed variant slots preserve the first eight reviewed queue-loss cases.
    # Each fifteen F08 cases includes early redelivery and isolated broker kills.
    if family == "F13":
        connector, contract, ops, faults, achievable, notes = _f13(rng, sid, index % 5)
    elif family == "F14":
        connector, contract, ops, faults, achievable, notes = _f14(rng, sid, index % 3)
    elif family == "F07" and index % 5 >= 3:
        connector, contract, ops, faults, achievable, notes = _f07_after_escalation(
            rng, sid, ("payments", "crm")[index % 5 - 3])
    elif family == "F16" and index % 5 >= 3:
        connector, contract, ops, faults, achievable, notes = _f16_deadline(
            rng, sid, ("payments", "crm")[index % 5 - 3])
    elif _email_slot(family, index):
        connector, contract, ops, faults, achievable, notes = _email_cell(
            family, index, rng, sid)
    elif family == "F08" and index % 15 >= 12:
        connector, contract, ops, faults, achievable, notes = _f08_kill(
            rng, sid, ("payments", "crm", "email")[index % 15 - 12])
    elif family == "F08" and index % 15 >= 8:
        connector, contract, ops, faults, achievable, notes = _f08_redelivery(
            rng, sid, ("payments", "crm", "email")[(index % 15 - 8) % 3])
    elif family in TEMPLATES:
        connector, contract, ops, faults, achievable, notes = TEMPLATES[
            family](rng, sid)
    else:
        connector, contract, ops, faults, achievable, notes = _combined(
            rng, sid, family)
    return Scenario(
        scenario_id=sid, split=split, family=family, connector=connector,
        contract_version=contract, seed=seed, operations=tuple(ops),
        faults=tuple(faults), completion_achievable=achievable,
        settle_seconds=max(SETTLE.get(f, 15.0) for f in family.replace(
            "x8", "").split("+")),
        notes=notes,
        workflow=(WorkflowSpec(ops[0].resource_id, _resource("crm", sid, 0),
            f"sku_{ops[0].amount_minor}", compensation_contract=(
                "q-v2-eventual" if any(f.get("mode") == "hidden_response" for f in faults)
                else "q-v2")) if any(f["kind"] == "workflow" for f in faults) else None),
        review_mode=("concurrent", "stale", "protected_retry")[index % 3] if family == "F14" else None,
    )


def corpus(split, counts, seed):
    """counts: {family: n}. Deterministic order: family, then index."""
    return [generate(split, family, index, seed)
            for family, n in counts.items() for index in range(n)]


def digest(scenarios):
    return hashlib.sha256("\n".join(s.to_json() for s in scenarios)
                          .encode()).hexdigest()
