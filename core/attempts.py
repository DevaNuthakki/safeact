"""Pure structural checks for operation attempt coverage.

This module does not authenticate provider evidence or
establish that a database inventory is complete.
"""

from dataclasses import dataclass
from enum import Enum

from core.contracts import OperationIdentity


class AttemptOutcome(str, Enum):
    NEVER_DISPATCHED = "never_dispatched"
    MAY_HAVE_DISPATCHED = "may_have_dispatched"
    PROVEN_NOT_APPLIED = "proven_not_applied"
    CONFIRMED_APPLIED = "confirmed_applied"


class CoverageVerdict(str, Enum):
    COMPLETE_NEGATIVE = "complete_negative"
    INCOMPLETE = "incomplete"
    BINDING_CONFLICT = "binding_conflict"
    CONTRADICTORY_APPLIED = "contradictory_applied"


@dataclass(frozen=True, slots=True)
class AttemptObservation:
    attempt_id: str
    operation_id: str
    connector_id: str
    provider_account_id: str
    outcome: AttemptOutcome
    proof_reference: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "attempt_id",
            "operation_id",
            "connector_id",
            "provider_account_id",
        ):
            value = getattr(self, name)

            if (
                type(value) is not str
                or not value
                or value != value.strip()
            ):
                raise ValueError(
                    f"{name} must be normalized and nonblank"
                )

        if not isinstance(self.outcome, AttemptOutcome):
            raise ValueError("invalid attempt outcome")

        terminal = {
            AttemptOutcome.NEVER_DISPATCHED,
            AttemptOutcome.PROVEN_NOT_APPLIED,
            AttemptOutcome.CONFIRMED_APPLIED,
        }

        if self.outcome in terminal:
            if (
                type(self.proof_reference) is not str
                or not self.proof_reference.strip()
            ):
                raise ValueError(
                    "terminal observation requires proof reference"
                )

        elif self.proof_reference is not None:
            raise ValueError(
                "pending observation cannot claim terminal proof"
            )


@dataclass(frozen=True, slots=True)
class AttemptInventory:
    operation_id: str
    connector_id: str
    provider_account_id: str
    contract_version: str
    is_complete: bool
    attempts: tuple[AttemptObservation, ...]

    def __post_init__(self) -> None:
        for name in (
            "operation_id",
            "connector_id",
            "provider_account_id",
            "contract_version",
        ):
            value = getattr(self, name)

            if (
                type(value) is not str
                or not value
                or value != value.strip()
            ):
                raise ValueError(
                    f"{name} must be normalized and nonblank"
                )

        if type(self.is_complete) is not bool:
            raise ValueError(
                "inventory completeness must be explicit"
            )

        if type(self.attempts) is not tuple:
            raise ValueError("attempt inventory must be a tuple")

        ids = set()

        for attempt in self.attempts:
            if not isinstance(attempt, AttemptObservation):
                raise ValueError("invalid attempt inventory entry")

            if attempt.attempt_id in ids:
                raise ValueError("duplicate attempt identity")

            ids.add(attempt.attempt_id)


def assess_negative_coverage(
    inventory: AttemptInventory | None,
    operation: OperationIdentity,
    pinned_contract_version: str,
) -> CoverageVerdict:
    """Check whether structural negative coverage is complete.

    Only trusted service components may eventually establish
    real inventory completeness and evidence authenticity.
    """

    if inventory is None:
        return CoverageVerdict.INCOMPLETE

    if (
        inventory.operation_id != operation.operation_id
        or inventory.connector_id != operation.connector_id
        or inventory.provider_account_id
        != operation.provider_account_id
        or inventory.contract_version
        != pinned_contract_version
    ):
        return CoverageVerdict.BINDING_CONFLICT

    for attempt in inventory.attempts:
        if (
            attempt.operation_id != operation.operation_id
            or attempt.connector_id != operation.connector_id
            or attempt.provider_account_id
            != operation.provider_account_id
        ):
            return CoverageVerdict.BINDING_CONFLICT

    if any(
        attempt.outcome is AttemptOutcome.CONFIRMED_APPLIED
        for attempt in inventory.attempts
    ):
        return CoverageVerdict.CONTRADICTORY_APPLIED

    if not inventory.is_complete or not inventory.attempts:
        return CoverageVerdict.INCOMPLETE

    if any(
        attempt.outcome is AttemptOutcome.MAY_HAVE_DISPATCHED
        for attempt in inventory.attempts
    ):
        return CoverageVerdict.INCOMPLETE

    return CoverageVerdict.COMPLETE_NEGATIVE
