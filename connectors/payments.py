"""Payments connector adapter over the mock payments provider.

Connector logic only: it classifies refund responses and normalizes
lookups under the operation's registered profile. It talks to the
provider surface (``refund``, ``lookup``) and nothing else.
"""

import json

from core.connector_profiles import PAYMENTS
from core.contracts import EvidenceKind, canonical_payload_hash
from connectors.base import (
    DispatchKind,
    DispatchResult,
    RawLookup,
    normalize_lookup,
)
from connectors.payments_mock import (
    IdempotencyConflict,
    LookupUnavailable,
    ProviderInFlight,
    ProviderRateLimited,
    ProviderRejected,
    ProviderServerError,
    ProviderTimeout,
)


# Action type -> provider method. One key space for both: a key binds
# to exactly one request and its parameters.
ACTIONS = {
    "refund_payment": "refund",
    "charge_shipping_fee": "charge",
}

# Failures that leave the effect unknown.
NO_RESPONSE_ERRORS = (ProviderTimeout, ConnectionError, OSError)


def _seconds(value):
    return f"{max(0.0, min(float(value), 999999.0)):.3f}"


class PaymentsConnector:

    connector_id = PAYMENTS

    def __init__(self, provider):
        self._provider = provider

    def accepts(self, intent):
        return (
            intent.connector_id == PAYMENTS
            and intent.action_type in ACTIONS
            and bool(intent.provider_idempotency_key)
        )

    def dispatch(self, intent, *, operation_id, timeout=None):
        params = json.loads(intent.payload_canonical)

        send = getattr(self._provider, ACTIONS[intent.action_type])
        try:
            receipt = send(
                intent.provider_idempotency_key, params, timeout=timeout
            )
        except ProviderRejected as rejected:
            return DispatchResult(
                DispatchKind.NOT_APPLIED, "provider_rejected",
                rejected.reference, retryable=rejected.retryable,
            )
        except ProviderRateLimited as limited:
            # This contract documents its 429 as answered before
            # execution, like Stripe: nothing applied, retry later.
            facts = {"rate_limited": "true"}
            if limited.retry_after is not None:
                facts["retry_after"] = _seconds(limited.retry_after)
            return DispatchResult(
                DispatchKind.NOT_APPLIED, "provider_rejected",
                "provider:429", retryable=True, provider_facts=facts,
            )
        except IdempotencyConflict:
            return DispatchResult(
                DispatchKind.AMBIGUOUS, "ambiguous_key_conflict",
                observed=EvidenceKind.INCONCLUSIVE,
            )
        except ProviderInFlight:
            return DispatchResult(
                DispatchKind.AMBIGUOUS, "provider_in_flight_conflict",
                observed=EvidenceKind.INCONCLUSIVE,
            )
        except ProviderServerError:
            # A 5xx may follow a committed effect (F03).
            return DispatchResult(
                DispatchKind.AMBIGUOUS, "ambiguous_error_after_send"
            )
        except NO_RESPONSE_ERRORS:
            return DispatchResult(
                DispatchKind.AMBIGUOUS, "ambiguous_no_response"
            )

        return DispatchResult(
            DispatchKind.APPLIED, "provider_confirmed", receipt.refund_id
        )

    def lookup(self, target, profile):
        key = target.provider_idempotency_key
        if not key:
            raw = RawLookup("unavailable", "")
        else:
            try:
                found = self._provider.lookup(key)
            except LookupUnavailable:
                raw = RawLookup("unavailable", key)
            else:
                raw = RawLookup(
                    found.status, key, found.refund_id,
                    None if found.params is None
                    else canonical_payload_hash(found.params),
                )
        return normalize_lookup(profile, target, raw)
