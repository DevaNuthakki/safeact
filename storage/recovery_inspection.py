"""Negative-only authenticated recovery inspection.

This component performs no provider dispatch and never
issues positive retry authorization.

It depends on AuthenticatedAdmissionService.observe(), which
verifies identity and resolves current admission authority
before accessing database admission evidence.

A database admission is not provider-effect evidence.

IMPORTANT:
    RecoveryInspection is a diagnostic result, not an
    authentication credential or a dispatch capability.

    This module alone does not enforce rules on an external
    provider worker that does not invoke it.
"""

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from storage.authenticated_submission import (
    AuthenticatedAdmissionService,
)
from storage.submission import (
    IntentObservation,
    IntentObservationStatus,
    SubmissionStateUncertain,
)


class RecoveryInspectionVerdict(str, Enum):
    HOLD_ADMISSION_UNKNOWN = "HOLD_ADMISSION_UNKNOWN"
    HOLD_PROVIDER_UNVERIFIED = "HOLD_PROVIDER_UNVERIFIED"


@dataclass(frozen=True, slots=True)
class RecoveryInspection:
    """Read-only observation, deliberately without allow grants."""

    verdict: RecoveryInspectionVerdict
    operation_id: UUID | None

    def __post_init__(self):
        if type(self.verdict) is not RecoveryInspectionVerdict:
            raise ValueError("Invalid inspection verdict")

        if self.verdict is RecoveryInspectionVerdict.HOLD_ADMISSION_UNKNOWN:
            if self.operation_id is not None:
                raise ValueError(
                    "Unresolved admission must not assert an operation"
                )

        elif type(self.operation_id) is not UUID:
            raise ValueError(
                "Recorded admission requires a UUID operation identity"
            )

    @property
    def retry_allowed(self) -> bool:
        """Never grant a provider retry from admission observation."""
        return False

    @property
    def provider_dispatch_authorized(self) -> bool:
        """This component has no provider-dispatch authority."""
        return False


def inspect_recovery_admission(
    *,
    admission_service: AuthenticatedAdmissionService,
    token: str,
    request,
) -> RecoveryInspection:
    """Inspect admission using the authenticated service.

    The authenticated service, not this caller, resolves
    identity and grants. No actor, tenant, grant collection,
    retry flag, or provider evidence is accepted here.

    Authentication/authorization failures propagate.

    Database read failures propagate.

    Malformed or unsupported observations fail closed.
    """

    if type(admission_service) is not AuthenticatedAdmissionService:
        raise TypeError(
            "Configured authenticated admission service required"
        )

    # Never bypass this service by calling the lower-level
    # observation function with caller-created dataclasses.
    observed = admission_service.observe(
        token=token,
        request=request,
    )

    if type(observed) is not IntentObservation:
        raise SubmissionStateUncertain(
            "Authenticated admission observation unavailable"
        )

    if observed.status is IntentObservationStatus.UNKNOWN:
        if observed.operation_id is not None:
            raise SubmissionStateUncertain(
                "Unresolved admission supplied operation identity"
            )

        return RecoveryInspection(
            verdict=(
                RecoveryInspectionVerdict.HOLD_ADMISSION_UNKNOWN
            ),
            operation_id=None,
        )

    if observed.status is IntentObservationStatus.CONFIRMED_RECORDED:
        if type(observed.operation_id) is not UUID:
            raise SubmissionStateUncertain(
                "Recorded admission lacks UUID identity"
            )

        # A committed operation is not evidence of
        # provider-side application or non-application.
        return RecoveryInspection(
            verdict=(
                RecoveryInspectionVerdict.HOLD_PROVIDER_UNVERIFIED
            ),
            operation_id=observed.operation_id,
        )

    raise SubmissionStateUncertain(
        "Unsupported admission observation status"
    )
