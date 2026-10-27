"""Pure, fail-closed recovery decisions.

No network calls or database mutations occur here.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from core.attempts import (
    AttemptInventory,
    CoverageVerdict,
    assess_negative_coverage,
)

from core.contracts import (
    ConnectorContract,
    Evidence,
    EvidenceKind,
    OperationIdentity,
)


class DecisionKind(str, Enum):
    REJECT_CONFLICT = "reject_conflict"
    CONFIRM_APPLIED = "confirm_applied"
    CONFIRM_NOT_APPLIED = "confirm_not_applied"
    RETRY_SAME_KEY = "retry_same_key"
    RETRY_AFTER_PROOF = "retry_after_proof"
    VERIFY_LATER = "verify_later"
    ESCALATE_UNRESOLVED = "escalate_unresolved"


@dataclass(frozen=True, slots=True)
class Decision:
    kind: DecisionKind
    reason: str


@dataclass(frozen=True, slots=True)
class RecoveryRequest:
    original: OperationIdentity
    candidate: OperationIdentity
    original_provider_key: str | None
    candidate_provider_key: str | None
    contract: ConnectorContract
    pinned_contract_version: str
    evidence: Evidence
    first_dispatch_at: datetime
    investigation_deadline: datetime
    now: datetime
    remaining_retries: int
    retry_authorized: bool
    attempt_inventory: AttemptInventory | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.original, OperationIdentity)
            or not isinstance(self.candidate, OperationIdentity)
        ):
            raise ValueError(
                "operation identities are required"
            )

        if (
            not isinstance(self.contract, ConnectorContract)
            or not isinstance(self.evidence, Evidence)
        ):
            raise ValueError(
                "typed connector contract and evidence are required"
            )

        if (
            type(self.pinned_contract_version) is not str
            or not self.pinned_contract_version.strip()
        ):
            raise ValueError(
                "pinned contract version is required"
            )

        for key in (
            self.original_provider_key,
            self.candidate_provider_key,
        ):
            if (
                key is not None
                and (
                    type(key) is not str
                    or not key.strip()
                )
            ):
                raise ValueError(
                    "provider keys must be nonblank or None"
                )

        for name in (
            "first_dispatch_at",
            "investigation_deadline",
            "now",
        ):
            stamp = getattr(self, name)

            if (
                not isinstance(stamp, datetime)
                or stamp.tzinfo is None
                or stamp.utcoffset() is None
            ):
                raise ValueError(
                    f"{name} must be timezone-aware"
                )

        if self.investigation_deadline < self.first_dispatch_at:
            raise ValueError(
                "investigation deadline predates first dispatch"
            )

        if self.now < self.first_dispatch_at:
            raise ValueError(
                "decision time predates first dispatch"
            )

        if (
            type(self.remaining_retries) is not int
            or self.remaining_retries < 0
        ):
            raise ValueError(
                "remaining retries must be a nonnegative integer"
            )

        if type(self.retry_authorized) is not bool:
            raise ValueError(
                "retry authorization must be explicit"
            )

        if (
            self.attempt_inventory is not None
            and not isinstance(
                self.attempt_inventory,
                AttemptInventory,
            )
        ):
            raise ValueError(
                "attempt inventory must use the domain contract"
            )


def decide_recovery(request: RecoveryRequest) -> Decision:
    """Choose the next step from normalized provider evidence.

    Constructing Evidence is NOT proof of authenticity.

    The service must enforce evidence provenance,
    operation ownership, contract version, and
    authorization before invoking this policy.
    """

    if request.original != request.candidate:
        return Decision(
            DecisionKind.REJECT_CONFLICT,
            "operation_identity_mismatch",
        )

    if (
        request.original_provider_key
        != request.candidate_provider_key
    ):
        return Decision(
            DecisionKind.REJECT_CONFLICT,
            "provider_key_mismatch",
        )

    if (
        request.contract.connector_id
        != request.original.connector_id
        or request.contract.version
        != request.pinned_contract_version
    ):
        return Decision(
            DecisionKind.REJECT_CONFLICT,
            "connector_contract_mismatch",
        )

    if (
        request.evidence.connector_id
        != request.original.connector_id
        or request.evidence.operation_id
        != request.original.operation_id
        or request.evidence.provider_account_id
        != request.original.provider_account_id
        or request.evidence.contract_version
        != request.pinned_contract_version
    ):
        return Decision(
            DecisionKind.REJECT_CONFLICT,
            "evidence_binding_mismatch",
        )

    if request.evidence.kind is EvidenceKind.APPLIED:
        return Decision(
            DecisionKind.CONFIRM_APPLIED,
            "provider_effect_confirmed",
        )

    can_retry = (
        request.retry_authorized
        and request.remaining_retries > 0
    )

    if (
        request.evidence.kind
        is EvidenceKind.PROVEN_NOT_APPLIED
    ):
        coverage = assess_negative_coverage(
            request.attempt_inventory,
            request.original,
            request.pinned_contract_version,
        )

        if coverage in (
            CoverageVerdict.BINDING_CONFLICT,
            CoverageVerdict.CONTRADICTORY_APPLIED,
        ):
            return Decision(
                DecisionKind.REJECT_CONFLICT,
                "negative_evidence_coverage_conflict",
            )

        if coverage is not CoverageVerdict.COMPLETE_NEGATIVE:
            if request.now >= request.investigation_deadline:
                return Decision(
                    DecisionKind.ESCALATE_UNRESOLVED,
                    "negative_evidence_coverage_incomplete",
                )

            # Proof for one attempt is never weaker than NOT_FOUND: an
            # earlier attempt left unresolved still has the native key
            # (D-F16-0002: escalated with the key window open).
            if _native_key_protects(request, can_retry):
                return Decision(
                    DecisionKind.RETRY_SAME_KEY,
                    "provider_key_still_protected",
                )

            return Decision(
                DecisionKind.VERIFY_LATER,
                "negative_evidence_coverage_incomplete",
            )

        if can_retry:
            return Decision(
                DecisionKind.RETRY_AFTER_PROOF,
                "original_cannot_later_apply",
            )

        return Decision(
            DecisionKind.CONFIRM_NOT_APPLIED,
            "original_cannot_later_apply_no_retry",
        )

    # NOT_FOUND and INCONCLUSIVE do not prove rejection.
    if request.now >= request.investigation_deadline:
        return Decision(
            DecisionKind.ESCALATE_UNRESOLVED,
            "investigation_deadline_reached",
        )

    if _native_key_protects(request, can_retry):
        return Decision(
            DecisionKind.RETRY_SAME_KEY,
            "provider_key_still_protected",
        )

    return Decision(
        DecisionKind.VERIFY_LATER,
        "insufficient_safe_retry_evidence",
    )


def _native_key_protects(request: RecoveryRequest, can_retry: bool) -> bool:
    """A same-key retry is protected before first fence + retention - margin."""

    native = request.contract.native_idempotency

    return (
        can_retry
        and native is not None
        and native.protects_concurrent_same_key
        and native.binds_parameters
        and request.original_provider_key is not None
        and request.now
        < (
            request.first_dispatch_at
            + native.retention
            - native.safety_margin
        )
    )
