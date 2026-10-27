"""Durable operation lifecycle: states and legal transitions.

This is the domain source of the transition table. PostgreSQL is
seeded by the frozen per-migration modules in ``storage.lifecycle_sql``;
a test proves the union of their seeds equals LEGAL_TRANSITIONS, so a
new pair here needs a new migration. Legality does not grant
authority: each transition is reachable only through its protected
database function.
"""

from enum import Enum


class LifecycleState(str, Enum):
    RECORDED = "RECORDED"
    CLAIMED = "CLAIMED"
    DISPATCHING = "DISPATCHING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_CONFIRMED = "FAILED_CONFIRMED"
    UNKNOWN = "UNKNOWN"
    VERIFYING = "VERIFYING"
    RETRY_ELIGIBLE = "RETRY_ELIGIBLE"
    ESCALATED_UNRESOLVED = "ESCALATED_UNRESOLVED"
    CANCELLED_BEFORE_CLAIM = "CANCELLED_BEFORE_CLAIM"


S = LifecycleState

# Blueprint section 05 plus documented deviations (CLAUDE.md):
# VERIFYING -> FAILED_CONFIRMED (proven not applied, no retry; 0003);
# ESCALATED_UNRESOLVED -> VERIFYING (reopen on new decisive evidence
# recorded after the escalation; 0005); CLAIMED -> RETRY_ELIGIBLE or
# UNKNOWN (abandoned claim, exit chosen by attempt history; 0006);
# RETRY_ELIGIBLE -> ESCALATED_UNRESOLVED (reclaim blocked by the
# abandoned-claim limit or attempt budget: human review; 0008);
# RECORDED -> CANCELLED_BEFORE_CLAIM (a requested cancellation, only as
# the first transition with no claim or attempt ever recorded; 0012).
LEGAL_TRANSITIONS = frozenset({
    (S.RECORDED, S.CLAIMED),
    (S.CLAIMED, S.DISPATCHING),
    (S.DISPATCHING, S.SUCCEEDED),
    (S.DISPATCHING, S.FAILED_CONFIRMED),
    (S.DISPATCHING, S.UNKNOWN),
    (S.UNKNOWN, S.VERIFYING),
    (S.VERIFYING, S.SUCCEEDED),
    (S.VERIFYING, S.FAILED_CONFIRMED),
    (S.VERIFYING, S.RETRY_ELIGIBLE),
    (S.VERIFYING, S.UNKNOWN),
    (S.VERIFYING, S.ESCALATED_UNRESOLVED),
    (S.RETRY_ELIGIBLE, S.CLAIMED),
    (S.ESCALATED_UNRESOLVED, S.VERIFYING),
    (S.CLAIMED, S.RETRY_ELIGIBLE),
    (S.CLAIMED, S.UNKNOWN),
    (S.RETRY_ELIGIBLE, S.ESCALATED_UNRESOLVED),
    (S.RECORDED, S.CANCELLED_BEFORE_CLAIM),
})

# Completed work has no exits. ESCALATED_UNRESOLVED is unresolved work,
# never completed work; its only exit is an evidence-gated reopen.
COMPLETED_STATES = frozenset({S.SUCCEEDED, S.FAILED_CONFIRMED})
UNRESOLVED_STATES = frozenset({S.ESCALATED_UNRESOLVED})
# Never dispatched, by request. Neither completed business work nor
# unresolved; accounted for in its own bucket (A5).
CANCELLED_STATES = frozenset({S.CANCELLED_BEFORE_CLAIM})

# Outcomes a claim owner may record after its own dispatch.
DISPATCH_OUTCOMES = frozenset({
    S.SUCCEEDED,
    S.FAILED_CONFIRMED,
    S.UNKNOWN,
})

# Moves reachable only through the recovery function.
RECOVERY_TRANSITIONS = frozenset({
    (S.DISPATCHING, S.UNKNOWN),
    (S.UNKNOWN, S.VERIFYING),
    (S.VERIFYING, S.SUCCEEDED),
    (S.VERIFYING, S.FAILED_CONFIRMED),
    (S.VERIFYING, S.RETRY_ELIGIBLE),
    (S.VERIFYING, S.UNKNOWN),
    (S.VERIFYING, S.ESCALATED_UNRESOLVED),
    (S.ESCALATED_UNRESOLVED, S.VERIFYING),
    (S.CLAIMED, S.RETRY_ELIGIBLE),
    (S.CLAIMED, S.UNKNOWN),
    (S.RETRY_ELIGIBLE, S.ESCALATED_UNRESOLVED),
})

# Exits from CLAIMED that the claim owner may take at once
# (safeact_release_claim); the recovery role only after the lease.
RELEASE_TRANSITIONS = frozenset({
    (S.CLAIMED, S.RETRY_ELIGIBLE),
    (S.CLAIMED, S.UNKNOWN),
})

# Reclaim of retry-eligible work, through the claim function (0007),
# within the attempt budget and the abandoned-claim cap.
RECLAIM_TRANSITIONS = frozenset({(S.RETRY_ELIGIBLE, S.CLAIMED)})

# A requested cancellation, through the API's cancel function only
# (0012), as the first transition of a never-claimed operation.
CANCEL_TRANSITIONS = frozenset({(S.RECORDED, S.CANCELLED_BEFORE_CLAIM)})

# Why a dispatch outcome is UNKNOWN (enforced in SQL from 0007). A
# definitive rejection awaiting a retry decision is reported apart from
# genuinely ambiguous outcomes (blueprint §09 keeps them separate).
RETRY_PENDING_REASON = "rejected_before_execution_retry_pending"
# A confirmed failure whose only cause is that no reviewer approved a
# required retry before the investigation deadline (0015).
APPROVAL_EXPIRED_REASON = "retry_approval_expired"
AMBIGUOUS_REASONS = frozenset({
    "ambiguous_no_response",
    "ambiguous_error_after_send",
    "ambiguous_key_conflict",
    "provider_in_flight_conflict",
    "request_deadline_elapsed_before_send",
    # 0018: a 429 the contract does not document as pre-execution.
    "ambiguous_rate_limited",
})


def is_legal(from_state, to_state):
    return (
        LifecycleState(from_state),
        LifecycleState(to_state),
    ) in LEGAL_TRANSITIONS
