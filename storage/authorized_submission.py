"""Internal authorization-to-admission adapter.

The server-side caller MUST supply authenticated context and
authoritative grants. Python dataclasses are not credentials.

This module neither authenticates clients nor dispatches effects.
"""

import json

from sqlalchemy.engine import Engine

from core.admission_authorization import (
    AdmissionGrant,
    AuthorizedIntent,
    InvalidAdmissionRequest,
    RequestedIntent,
    TrustedActorContext,
    authorize_intent,
)
from core.contracts import canonical_payload_bytes
from storage.submission import (
    IntentObservation,
    IntentSubmission,
    SubmissionReceipt,
    observe_intent,
    submit_intent,
)


def _submission_from_authorized(
    authorized: AuthorizedIntent,
) -> IntentSubmission:
    """Use only the authorized immutable payload snapshot."""
    if type(authorized) is not AuthorizedIntent:
        raise InvalidAdmissionRequest(
            "Authorized intent is required"
        )

    try:
        wire = authorized.payload_canonical.decode("utf-8")
        payload = json.loads(wire)

        if (
            type(payload) is not dict
            or canonical_payload_bytes(payload)
            != authorized.payload_canonical
        ):
            raise ValueError(
                "Authorized payload is not canonical"
            )

    except (ValueError, TypeError, UnicodeError) as exc:
        raise InvalidAdmissionRequest(
            "Invalid authorized payload snapshot"
        ) from exc

    return IntentSubmission(
        tenant_id=authorized.tenant_id,
        actor_id=authorized.actor_id,
        request_key=authorized.request_key,
        action_type=authorized.action_type,
        resource_id=authorized.resource_id,
        provider_account_id=authorized.provider_account_id,
        connector_id=authorized.connector_id,
        contract_version=authorized.contract_version,
        payload=payload,
        provider_idempotency_key=(
            authorized.provider_idempotency_key
        ),
    )


def submit_scoped_intent(
    engine: Engine,
    context: TrustedActorContext,
    grants: tuple[AdmissionGrant, ...],
    request: RequestedIntent,
) -> SubmissionReceipt:
    """Authorize before entering the database submission path.

    Authorization inputs must originate in trusted server code.
    Database commit ambiguity is propagated, never auto-retried.
    """
    authorized = authorize_intent(
        context, grants, request
    )

    submission = _submission_from_authorized(
        authorized
    )

    return submit_intent(engine, submission)


def observe_scoped_intent(
    engine: Engine,
    context: TrustedActorContext,
    grants: tuple[AdmissionGrant, ...],
    request: RequestedIntent,
) -> IntentObservation:
    """Authorize before performing an admission observation.

    UNKNOWN is never interpreted as permission to retry.
    """
    authorized = authorize_intent(
        context, grants, request
    )

    submission = _submission_from_authorized(
        authorized
    )

    return observe_intent(engine, submission)
