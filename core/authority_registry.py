"""Server-configured issuer/subject to actor/scope resolution.

Only trusted server code may construct this registry.
This module does not authenticate tokens, verify ownership
against an external service, or confer unforgeable privileges.
"""

from dataclasses import dataclass
from types import MappingProxyType

from core.admission_authorization import (
    AdmissionGrant,
    TrustedActorContext,
)
from core.verified_identity import VerifiedPrincipal


class AuthorityConfigurationError(ValueError):
    """The server-side authority configuration is invalid."""


class AuthorityResolutionDenied(PermissionError):
    """No active authorized actor mapping is available."""


@dataclass(frozen=True, slots=True)
class ActorAuthorityRecord:
    issuer: str
    subject: str
    tenant_id: str
    actor_id: str
    grants: tuple[AdmissionGrant, ...]
    enabled: bool = True


_LIMITS = {
    "issuer": 512,
    "subject": 255,
    "tenant_id": 128,
    "actor_id": 128,
    "action_type": 128,
    "connector_id": 128,
    "provider_account_id": 255,
    "resource_id": 255,
    "contract_version": 64,
}


def _valid_scope_text(value: object, limit: int) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value) <= limit
        and value == value.strip()
        and "*" not in value
        and not any(
            ord(char) < 32 or ord(char) == 127
            for char in value
        )
    )


class AuthorityRegistry:
    """Immutable local snapshot of trusted actor records.

    Registry records must come from trusted deployment
    configuration or a future authoritative policy service.

    A verified JWT subject maps to exactly one internal
    tenant and actor in this initial contract.
    """

    def __init__(
        self,
        *,
        audience: str,
        records: tuple[ActorAuthorityRecord, ...],
    ) -> None:
        if not _valid_scope_text(audience, 512):
            raise AuthorityConfigurationError(
                "Invalid expected audience"
            )

        if (
            type(records) is not tuple
            or len(records) > 256
        ):
            raise AuthorityConfigurationError(
                "Invalid authority record collection"
            )

        by_principal = {}
        actor_owners = set()

        for record in records:
            if type(record) is not ActorAuthorityRecord:
                raise AuthorityConfigurationError(
                    "Invalid actor record"
                )

            for name in (
                "issuer",
                "subject",
                "tenant_id",
                "actor_id",
            ):
                if not _valid_scope_text(
                    getattr(record, name),
                    _LIMITS[name],
                ):
                    raise AuthorityConfigurationError(
                        f"Invalid actor record field: {name}"
                    )

            if type(record.enabled) is not bool:
                raise AuthorityConfigurationError(
                    "Invalid actor enabled status"
                )

            if (
                type(record.grants) is not tuple
                or len(record.grants) > 128
            ):
                raise AuthorityConfigurationError(
                    "Invalid grant collection"
                )

            principal_key = (
                record.issuer,
                record.subject,
            )

            actor_key = (
                record.tenant_id,
                record.actor_id,
            )

            if principal_key in by_principal:
                raise AuthorityConfigurationError(
                    "Duplicate provider identity binding"
                )

            if actor_key in actor_owners:
                raise AuthorityConfigurationError(
                    "Internal actor has multiple identity bindings"
                )

            seen_grants = set()

            for grant in record.grants:
                if type(grant) is not AdmissionGrant:
                    raise AuthorityConfigurationError(
                        "Invalid exact-scope grant"
                    )

                for field in (
                    "tenant_id",
                    "actor_id",
                    "action_type",
                    "connector_id",
                    "provider_account_id",
                    "resource_id",
                    "contract_version",
                ):
                    if not _valid_scope_text(
                        getattr(grant, field),
                        _LIMITS[field],
                    ):
                        raise AuthorityConfigurationError(
                            f"Invalid grant field: {field}"
                        )

                if (
                    grant.tenant_id != record.tenant_id
                    or grant.actor_id != record.actor_id
                ):
                    raise AuthorityConfigurationError(
                        "Grant belongs to another actor or tenant"
                    )

                if grant in seen_grants:
                    raise AuthorityConfigurationError(
                        "Duplicate exact-scope grant"
                    )

                seen_grants.add(grant)

            by_principal[principal_key] = record
            actor_owners.add(actor_key)

        self._audience = audience
        self._records = MappingProxyType(by_principal)

    def resolve_verified(
        self,
        principal: VerifiedPrincipal,
    ) -> tuple[
        TrustedActorContext,
        tuple[AdmissionGrant, ...],
    ]:
        """Resolve an already verified identity.

        SECURITY: This method must only be invoked by a
        trusted server entry point immediately after actual
        token verification. A dataclass can be forged by
        Python code and is not an authentication credential.
        """
        if type(principal) is not VerifiedPrincipal:
            raise AuthorityResolutionDenied(
                "Verified identity is required"
            )

        if principal.audience != self._audience:
            raise AuthorityResolutionDenied(
                "Identity audience is not authorized"
            )

        record = self._records.get(
            (principal.issuer, principal.subject)
        )

        if (
            record is None
            or not record.enabled
            or not record.grants
        ):
            raise AuthorityResolutionDenied(
                "No active authorized actor mapping"
            )

        return (
            TrustedActorContext(
                tenant_id=record.tenant_id,
                actor_id=record.actor_id,
            ),
            record.grants,
        )
