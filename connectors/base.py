"""Connector port: what the worker and reconciler may ask of a connector.

``dispatch`` sends one request and classifies the response under the
connector's contract. ``lookup`` asks the provider what it can see and
returns evidence normalized under the profile. Neither ever reads the
harness's effects ledger.

Classification rule (blueprint §04): a status code or exception alone
is not proof. Only a confirmed effect is APPLIED; only a response the
connector contract defines as pre-execution rejection is NOT_APPLIED;
everything else is AMBIGUOUS.
"""

import re

from dataclasses import dataclass
from enum import Enum
from typing import Protocol
from uuid import UUID

from core.contracts import EvidenceKind, LookupSemantics


_FACT_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_FACTS = 8
MAX_FACT_LENGTH = 64


def _facts(value):
    """Non-decisive provider facts: a few short strings, recorded as
    evidence detail and never read by a decision (for example a Stripe
    refund's ``status``). Returned as sorted pairs so results stay frozen.
    """

    if value is None:
        return ()
    items = dict(value).items()
    if len(items) > MAX_FACTS or any(
        type(k) is not str or not _FACT_KEY.fullmatch(k)
        or type(v) is not str or len(v) > MAX_FACT_LENGTH
        for k, v in items
    ):
        raise ValueError("provider facts must be a few short strings")
    return tuple(sorted(items))


class DispatchKind(str, Enum):
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class DispatchResult:
    kind: DispatchKind
    reason: str
    reference: str | None = None
    # What the response alone shows when ambiguous (None or INCONCLUSIVE).
    observed: EvidenceKind | None = None
    # NOT_APPLIED only: the connector contract allows a later retry.
    retryable: bool = False
    # Recorded with the attempt evidence; never decides anything.
    provider_facts: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, "provider_facts",
                           _facts(self.provider_facts or None))
        if not isinstance(self.kind, DispatchKind):
            raise ValueError("unknown dispatch kind")
        if type(self.reason) is not str or not self.reason:
            raise ValueError("dispatch reason required")

        decisive = self.kind is not DispatchKind.AMBIGUOUS
        if decisive != (
            type(self.reference) is str and bool(self.reference.strip())
        ):
            raise ValueError("only decisive results carry a reference")
        if self.observed not in (None, EvidenceKind.INCONCLUSIVE) or (
            decisive and self.observed is not None
        ):
            raise ValueError("ambiguous results observe nothing decisive")
        if self.retryable and self.kind is not DispatchKind.NOT_APPLIED:
            raise ValueError("only a rejection can be retryable")


@dataclass(frozen=True, slots=True)
class LookupTarget:
    """What a lookup may correlate on: durable identity, never ledger."""

    operation_id: UUID
    provider_account_id: str
    provider_idempotency_key: str | None
    payload_sha256: str
    first_dispatch_at: object = None
    observed_at: object = None


@dataclass(frozen=True, slots=True)
class LookupResult:
    kind: EvidenceKind
    reference: str | None = None
    # Why a raw provider answer was downgraded, if it was.
    note: str | None = None
    # Recorded with the lookup evidence; never decides anything.
    provider_facts: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, "provider_facts",
                           _facts(self.provider_facts or None))
        if not isinstance(self.kind, EvidenceKind):
            raise ValueError("unknown evidence kind")
        decisive = self.kind in (
            EvidenceKind.APPLIED, EvidenceKind.PROVEN_NOT_APPLIED,
        )
        if decisive != (
            type(self.reference) is str and bool(self.reference.strip())
        ):
            raise ValueError("only decisive lookups carry a reference")


class Connector(Protocol):
    connector_id: str

    def accepts(self, intent) -> bool: ...

    def dispatch(self, intent, *, operation_id, timeout) -> DispatchResult: ...

    def lookup(self, target: LookupTarget, profile) -> LookupResult: ...


@dataclass(frozen=True, slots=True)
class RawLookup:
    """A provider's answer, before any contract is applied.

    status: "found", "absent", "in_progress" or "unavailable".
    payload_sha256: canonical hash of the parameters the provider holds
    for a found record, so a record bound to other parameters is never
    accepted as this operation's effect.
    """

    status: str
    correlation: str
    reference: str | None = None
    payload_sha256: str | None = None
    # Passed through to the evidence only (see _facts).
    provider_facts: tuple = ()


def completed_lookup(profile, target, result, observed_at):
    """Revalidate native negative proof at response completion, using DB time.

    An HTTP lookup may cross retention after its target snapshot was read.
    A found, correctly bound positive receipt remains evidence after expiry.
    This check never applies to dispatch's authoritative rejection responses.
    """
    native = profile.contract.native_idempotency
    if result.kind is not EvidenceKind.PROVEN_NOT_APPLIED or native is None:
        return result

    if (
        observed_at is not None
        and target.observed_at is not None
        and observed_at < target.observed_at
    ):
        return LookupResult(
            EvidenceKind.INCONCLUSIVE,
            note="lookup_clock_reversed",
            provider_facts=result.provider_facts,
        )

    if (
        observed_at is None
        or target.first_dispatch_at is None
        or observed_at
        >= target.first_dispatch_at + native.retention - native.safety_margin
    ):
        return LookupResult(
            EvidenceKind.NOT_FOUND,
            note="lookup_finished_after_key_window",
            provider_facts=result.provider_facts,
        )

    return result


def normalize_lookup(profile, target, raw):
    """Turn a raw provider answer into evidence under the profile.

    Absence proves non-application only when the contract says the
    lookup is STRONG and, for a native-key provider, the key is still
    inside its retention window. Anything outside the contract is
    INCONCLUSIVE, never decisive (F12).
    """

    contract = profile.contract

    if contract.lookup_semantics is LookupSemantics.UNAVAILABLE:
        return LookupResult(EvidenceKind.INCONCLUSIVE, note="no_lookup")

    if raw.status == "unavailable":
        return LookupResult(
            EvidenceKind.INCONCLUSIVE, note="lookup_unavailable"
        )

    if raw.status == "in_progress":
        return LookupResult(EvidenceKind.INCONCLUSIVE, note="in_progress")

    if raw.status == "found":
        if (
            not raw.reference
            or raw.payload_sha256 != target.payload_sha256
        ):
            return LookupResult(
                EvidenceKind.INCONCLUSIVE,
                note="out_of_contract_binding_mismatch",
            )
        return LookupResult(EvidenceKind.APPLIED, raw.reference,
                            provider_facts=raw.provider_facts)

    if raw.status != "absent":
        return LookupResult(
            EvidenceKind.INCONCLUSIVE, note="out_of_contract_status"
        )

    if contract.lookup_semantics is not LookupSemantics.STRONG:
        return LookupResult(EvidenceKind.NOT_FOUND, note="absent_eventual")

    native = contract.native_idempotency
    if native is not None and (
        target.first_dispatch_at is None
        or target.observed_at is None
        or target.observed_at
        >= target.first_dispatch_at + native.retention - native.safety_margin
    ):
        # The provider may have forgotten the key: absence is not proof.
        return LookupResult(
            EvidenceKind.NOT_FOUND, note="absent_after_key_retention"
        )

    return LookupResult(
        EvidenceKind.PROVEN_NOT_APPLIED, "lookup-absent:" + raw.correlation
    )
