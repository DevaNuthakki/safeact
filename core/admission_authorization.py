"""Pure SafeAct admission scope binding.

This module does not authenticate users or acquire grants.

The caller must be trusted server-side code that supplies
a verified actor context and authoritative grants.

No database writes, external effects, or dispatch occur here.
"""

from dataclasses import dataclass
from typing import Any

from core.contracts import canonical_payload_bytes


class AdmissionDenied(PermissionError):
    """The trusted context has no matching exact scope."""


class InvalidAdmissionRequest(ValueError):
    """The proposed admission request is malformed."""


@dataclass(frozen=True, slots=True)
class TrustedActorContext:
    tenant_id: str
    actor_id: str


@dataclass(frozen=True, slots=True)
class AdmissionGrant:
    tenant_id: str
    actor_id: str
    connector_id: str
    provider_account_id: str
    action_type: str
    resource_id: str
    contract_version: str


@dataclass(frozen=True, slots=True)
class RequestedIntent:
    request_key: str
    action_type: str
    resource_id: str
    provider_account_id: str
    connector_id: str
    contract_version: str
    payload: dict[str, Any]
    provider_idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class AuthorizedIntent:
    tenant_id: str
    actor_id: str
    request_key: str
    action_type: str
    resource_id: str
    provider_account_id: str
    connector_id: str
    contract_version: str
    payload_canonical: bytes
    provider_idempotency_key: str | None = None


_TEXT_LIMITS = {
    "tenant_id": 128,
    "actor_id": 128,
    "request_key": 255,
    "action_type": 128,
    "resource_id": 255,
    "provider_account_id": 255,
    "connector_id": 128,
    "contract_version": 64,
    "provider_idempotency_key": 255,
}


def _valid_text(value: object, limit: int) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value) <= limit
        and value == value.strip()
        and not any(
            ord(char) < 32 or ord(char) == 127
            for char in value
        )
    )


def authorize_intent(
    context: TrustedActorContext,
    grants: tuple[AdmissionGrant, ...],
    request: RequestedIntent,
) -> AuthorizedIntent:
    """Bind an exact allowed request to server-supplied identity.

    This is a policy decision, not proof of authentication.

    The grant collection MUST be resolved by trusted code.
    It must never be populated from client-provided scopes.

    No wildcard, fallback, or implicit permission exists.
    """
    if type(context) is not TrustedActorContext:
        raise AdmissionDenied(
            "Trusted actor context is unavailable"
        )

    if (
        type(grants) is not tuple
        or any(
            type(grant) is not AdmissionGrant
            for grant in grants
        )
    ):
        raise AdmissionDenied(
            "Authoritative grants are unavailable"
        )

    if type(request) is not RequestedIntent:
        raise InvalidAdmissionRequest(
            "A RequestedIntent is required"
        )

    for name in ("tenant_id", "actor_id"):
        value = getattr(context, name)

        if not _valid_text(value, _TEXT_LIMITS[name]):
            raise AdmissionDenied(
                "Invalid trusted identity"
            )

    for name in (
        "request_key",
        "action_type",
        "resource_id",
        "provider_account_id",
        "connector_id",
        "contract_version",
    ):
        value = getattr(request, name)

        if not _valid_text(value, _TEXT_LIMITS[name]):
            raise InvalidAdmissionRequest(
                f"Invalid {name}"
            )

    key = request.provider_idempotency_key

    if key is not None and not _valid_text(
        key, _TEXT_LIMITS["provider_idempotency_key"]
    ):
        raise InvalidAdmissionRequest(
            "Invalid provider idempotency key"
        )

    scope_fields = (
        "tenant_id",
        "actor_id",
        "connector_id",
        "provider_account_id",
        "action_type",
        "resource_id",
        "contract_version",
    )

    for grant in grants:
        for name in scope_fields:
            value = getattr(grant, name)

            # No wildcard syntax is supported.
            if (
                not _valid_text(value, _TEXT_LIMITS[name])
                or "*" in value
            ):
                raise AdmissionDenied(
                    "Invalid or wildcard grant"
                )

    expected = (
        context.tenant_id,
        context.actor_id,
        request.connector_id,
        request.provider_account_id,
        request.action_type,
        request.resource_id,
        request.contract_version,
    )

    matched = any(
        tuple(
            getattr(grant, name)
            for name in scope_fields
        ) == expected
        for grant in grants
    )

    if not matched:
        raise AdmissionDenied(
            "No exact authorization grant matches"
        )

    if type(request.payload) is not dict:
        raise InvalidAdmissionRequest(
            "Payload must be a JSON object"
        )

    try:
        encoded = canonical_payload_bytes(
            request.payload
        )
    except (ValueError, TypeError, UnicodeError) as exc:
        raise InvalidAdmissionRequest(
            "Invalid restricted JSON payload"
        ) from exc

    if not 2 <= len(encoded) <= 65536:
        raise InvalidAdmissionRequest(
            "Payload exceeds admission storage limits"
        )

    # Immutable bytes capture a snapshot of the proposal.
    # Mutating the original request's nested dictionary
    # cannot silently change this authorization result.
    return AuthorizedIntent(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        request_key=request.request_key,
        action_type=request.action_type,
        resource_id=request.resource_id,
        provider_account_id=request.provider_account_id,
        connector_id=request.connector_id,
        contract_version=request.contract_version,
        payload_canonical=encoded,
        provider_idempotency_key=(
            request.provider_idempotency_key
        ),
    )
