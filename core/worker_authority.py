"""Deny-only worker identity and assignment-scope review.

This module validates separately authenticated worker identity
against an immutable, trusted assignment snapshot.

It does NOT authorize:
- Database claiming or mutation.
- Attempt creation.
- Provider dispatch.
- Retry or recovery.
- Execution from a previously consumed invocation.

WorkerScopeReview is a diagnostic result, not a credential.

The caller must not construct the trusted assignment registry
from token claims or other untrusted request data.

This module does not verify that an operation exists in storage.
Database ownership and execution permissions remain separate,
unimplemented security boundaries.
"""

from dataclasses import dataclass
from types import MappingProxyType
from uuid import UUID

from core.verified_identity import IdentityVerifier


WORKER_AUDIENCE = "safeact-execution"


class WorkerAuthorityConfigurationError(ValueError):
    """Trusted worker configuration is invalid."""


class WorkerAccessDenied(Exception):
    """Worker identity or requested scope was not accepted."""


def _valid_identity_text(value, *, maximum):
    return (
        type(value) is str
        and 0 < len(value) <= maximum
        and value == value.strip()
        and all(
            33 <= ord(character) != 127
            for character in value
        )
    )


def _require_text(value, name, maximum):
    if not _valid_identity_text(
        value,
        maximum=maximum,
    ):
        raise ValueError(
            f"{name} must be a normalized nonblank identifier"
        )


def _require_scope(
    operation_id,
    tenant_id,
    connector_id,
    provider_account_id,
):
    if type(operation_id) is not UUID:
        raise ValueError("operation_id must be a UUID")

    _require_text(tenant_id, "tenant_id", 128)
    _require_text(connector_id, "connector_id", 128)
    _require_text(
        provider_account_id,
        "provider_account_id",
        255,
    )


@dataclass(frozen=True, slots=True)
class WorkerAssignment:
    """Server-provided assignment, never taken from a JWT."""

    subject: str
    operation_id: UUID
    tenant_id: str
    connector_id: str
    provider_account_id: str
    enabled: bool

    def __post_init__(self):
        _require_text(self.subject, "subject", 255)
        _require_scope(
            self.operation_id,
            self.tenant_id,
            self.connector_id,
            self.provider_account_id,
        )

        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a boolean")


@dataclass(frozen=True, slots=True)
class WorkerScopeRequest:
    """Untrusted scope being checked against server authority."""

    operation_id: UUID
    tenant_id: str
    connector_id: str
    provider_account_id: str

    def __post_init__(self):
        _require_scope(
            self.operation_id,
            self.tenant_id,
            self.connector_id,
            self.provider_account_id,
        )


@dataclass(frozen=True, slots=True)
class WorkerScopeReview:
    """Non-authorizing record of an identity/scope match.

    This dataclass is forgeable by ordinary Python callers.
    It must never be accepted as execution authority.
    """

    worker_subject: str
    operation_id: UUID
    tenant_id: str
    connector_id: str
    provider_account_id: str

    @property
    def provider_dispatch_authorized(self) -> bool:
        return False

    @property
    def retry_authorized(self) -> bool:
        return False

    @property
    def database_execution_authorized(self) -> bool:
        return False


class WorkerScopeReviewService:
    """Verify worker tokens and inspect trusted scope assignments.

    The worker audience is fixed to safeact-execution.

    A caller cannot disable the audience requirement by
    supplying a verifier configured for admission tokens.

    Assignment snapshots are not live revocation systems.
    """

    __slots__ = (
        "_verifier",
        "_trusted_issuer",
        "_assignments",
    )

    def __init__(
        self,
        *,
        identity_verifier: IdentityVerifier,
        trusted_issuer: str,
        assignments: tuple[WorkerAssignment, ...],
    ):
        if type(identity_verifier) is not IdentityVerifier:
            raise WorkerAuthorityConfigurationError(
                "A real trusted identity verifier is required"
            )

        if not _valid_identity_text(
            trusted_issuer,
            maximum=512,
        ):
            raise WorkerAuthorityConfigurationError(
                "A trusted issuer is required"
            )

        if type(assignments) is not tuple:
            raise WorkerAuthorityConfigurationError(
                "Assignments must be a trusted tuple"
            )

        snapshot = {}

        for assignment in assignments:
            if type(assignment) is not WorkerAssignment:
                raise WorkerAuthorityConfigurationError(
                    "Invalid trusted worker assignment"
                )

            key = (
                assignment.subject,
                assignment.operation_id,
            )

            if key in snapshot:
                raise WorkerAuthorityConfigurationError(
                    "Duplicate worker operation assignment"
                )

            snapshot[key] = assignment

        self._verifier = identity_verifier
        self._trusted_issuer = trusted_issuer

        self._assignments = MappingProxyType(snapshot)

    def inspect(
        self,
        *,
        token: str,
        request: WorkerScopeRequest,
    ) -> WorkerScopeReview:
        """Check a token against immutable assignment authority.

        A successful review is NOT permission to claim,
        modify storage, invoke a provider, or retry.
        """

        if (
            type(token) is not str
            or not token
            or type(request) is not WorkerScopeRequest
        ):
            raise WorkerAccessDenied(
                "Worker scope review denied"
            )

        principal = self._verifier.verify(token)

        if (
            principal.issuer != self._trusted_issuer
            or principal.audience != WORKER_AUDIENCE
        ):
            raise WorkerAccessDenied(
                "Worker scope review denied"
            )

        assignment = self._assignments.get(
            (
                principal.subject,
                request.operation_id,
            )
        )

        if (
            assignment is None
            or not assignment.enabled
            or assignment.tenant_id != request.tenant_id
            or assignment.connector_id != request.connector_id
            or assignment.provider_account_id
            != request.provider_account_id
        ):
            raise WorkerAccessDenied(
                "Worker scope review denied"
            )

        return WorkerScopeReview(
            worker_subject=assignment.subject,
            operation_id=assignment.operation_id,
            tenant_id=assignment.tenant_id,
            connector_id=assignment.connector_id,
            provider_account_id=assignment.provider_account_id,
        )
