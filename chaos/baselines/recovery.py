"""B3's conventional recovery decisions, independent of SafeAct lifecycle SQL.

Inputs are a committed job/attempt snapshot and the database's observed time.
The runtime must persist each actual connector result before using this policy;
neither a client nor a model supplies proof or approval flags. This module has
no provider/ledger/database access and never dispatches an action.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from hashlib import sha256

from core.connector_profiles import ConnectorProfile
from core.contracts import LookupSemantics


class Fact(str, Enum):
    AMBIGUOUS = "ambiguous"
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"


@dataclass(frozen=True)
class Attempt:
    ordinal: int
    fenced_at: datetime
    fact: Fact
    reference: str | None = None
    retryable: bool = False

    def __post_init__(self):
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise ValueError("Attempt ordinal must be positive")
        _aware(self.fenced_at)
        if not isinstance(self.fact, Fact):
            raise ValueError("Unknown attempt fact")
        if self.fact is Fact.AMBIGUOUS:
            if self.reference is not None or self.retryable:
                raise ValueError("Ambiguity carries no decisive reference or retry permission")
        elif type(self.reference) is not str or not self.reference.strip():
            raise ValueError("A decisive fact requires the provider's actual reference")
        if type(self.retryable) is not bool or (self.retryable and self.fact is not Fact.NOT_APPLIED):
            raise ValueError("Only authoritative non-application can be retryable")


@dataclass(frozen=True)
class Choice:
    action: str
    reason: str
    basis: str | None = None


def _aware(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Use timezone-aware database timestamps")


def native_window(profile, first_fence, now):
    """A fresh lease does not restart provider retention or create fencing."""
    _aware(now)
    if first_fence is None:
        return False
    _aware(first_fence)
    native = profile.contract.native_idempotency
    return bool(native and native.binds_parameters and native.protects_concurrent_same_key
                and first_fence <= now < first_fence + native.retention - native.safety_margin)


def choose(profile: ConnectorProfile, attempts: tuple[Attempt, ...], now: datetime, *,
           investigation_started: datetime, lookup_checked=False, not_before=None,
           review_required=False, approval_valid=False):
    """Decide after recording results, and again at a retry's dispatch fence.

    `lookup_checked` records this recovery pass, not an old empty lookup. A
    reviewed retry also needs a current, independently authenticated decision;
    the future runtime must check its persisted version/evidence binding.
    """
    _aware(now)
    _aware(investigation_started)
    if investigation_started > now:
        raise ValueError("Investigation starts no later than the observed database time")
    if any(type(v) is not bool for v in (lookup_checked, review_required, approval_valid)):
        raise ValueError("Recovery flags must be explicit booleans")
    if not isinstance(attempts, tuple) or any(not isinstance(a, Attempt) for a in attempts):
        raise ValueError("Use a typed committed attempt snapshot")
    if [a.ordinal for a in attempts] != list(range(1, len(attempts) + 1)):
        raise ValueError("Attempt history must be complete and ordered")
    if any(a.fenced_at > now for a in attempts) or any(
            a.fenced_at > b.fenced_at for a, b in zip(attempts, attempts[1:])):
        raise ValueError("Invalid dispatch chronology")
    if not_before is not None:
        _aware(not_before)
    if not attempts:
        return Choice("dispatch", "first_attempt")
    if any(a.fact is Fact.APPLIED for a in attempts):
        return Choice("complete", "provider_applied")

    all_rejected = all(a.fact is Fact.NOT_APPLIED for a in attempts)
    expired = now >= investigation_started + profile.investigation_window
    budget = len(attempts) < profile.max_attempts
    if all_rejected and not attempts[-1].retryable:
        return Choice("fail", "permanent_provider_rejection")
    if all_rejected and not budget:
        return Choice("fail", "authoritative_rejections_exhausted_budget")
    if all_rejected and expired:
        return Choice("fail", "review_expired" if review_required else "investigation_deadline")
    if all_rejected:
        basis = "all_attempts_not_applied"
    else:
        # A budget limit or deadline never turns an ambiguous attempt into proof
        # of failure, even if every later attempt was definitively rejected.
        if expired or not budget:
            return Choice("escalate", "investigation_deadline" if expired else "attempt_budget")
        if not lookup_checked and profile.contract.lookup_semantics is not LookupSemantics.UNAVAILABLE:
            return Choice("lookup", "investigate_ambiguous_attempt")
        if not native_window(profile, attempts[0].fenced_at, now):
            if profile.contract.lookup_semantics is LookupSemantics.UNAVAILABLE:
                return Choice("escalate", "no_safe_repeat_or_lookup")
            return Choice("wait", "unprotected_ambiguity")
        basis = "native_key_window"
    if review_required and not approval_valid:
        return Choice("review", "protected_retry_requires_human", basis)
    if not_before is not None and now < not_before:
        return Choice("wait", "retry_backoff", basis)
    return Choice("retry", "protected_repeat", basis)


def retry_delay(profile, identity, attempt_ordinal, *, retry_after=0):
    """Bounded deterministic full jitter; Retry-After is a minimum, not a cap."""
    if type(attempt_ordinal) is not int or attempt_ordinal < 1:
        raise ValueError("Positive attempt ordinal required")
    if type(identity) is not str or not identity:
        raise ValueError("Stable business identity required")
    if not isinstance(retry_after, (int, float)) or isinstance(retry_after, bool):
        raise ValueError("Retry-After must be finite nonnegative seconds")
    import math
    if not math.isfinite(retry_after) or retry_after < 0:
        raise ValueError("Retry-After must be finite nonnegative seconds")
    limits = profile.limits
    if limits is None:
        return timedelta(seconds=retry_after)
    base, cap = limits.backoff_base.total_seconds(), limits.backoff_cap.total_seconds()
    # Cap the exponent before evaluating it; an invalidly huge ordinal cannot
    # allocate a giant integer. Real dispatches are additionally budget-bounded.
    ceiling = min(cap, base * 2 ** min(attempt_ordinal - 1, 32))
    sample = int.from_bytes(sha256(f"{identity}:{attempt_ordinal}".encode()).digest()[:8], "big") / 2**64
    return timedelta(seconds=max(retry_after, ceiling * sample))
