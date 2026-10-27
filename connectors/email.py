"""Email connector adapter over the mock email provider.

The message id is the operation id; providers do not deduplicate on
it. There is no lookup: after an ambiguous dispatch nothing can be
proven, so the operation escalates and is never sent again (A3).
"""

import json

from core.connector_profiles import EMAIL
from connectors.base import (
    DispatchKind,
    DispatchResult,
    RawLookup,
    normalize_lookup,
)
from connectors.email_mock import (
    EmailRateLimited,
    EmailRejected,
    EmailServerError,
    EmailTimeout,
)


ACTIONS = frozenset({"send_refund_confirmation"})

NO_RESPONSE_ERRORS = (EmailTimeout, ConnectionError, OSError)


class EmailConnector:

    connector_id = EMAIL

    def __init__(self, provider):
        self._provider = provider

    def accepts(self, intent):
        return (
            intent.connector_id == EMAIL and intent.action_type in ACTIONS
        )

    def dispatch(self, intent, *, operation_id, timeout=None):
        params = json.loads(intent.payload_canonical)

        try:
            delivery_id = self._provider.send(
                str(operation_id), params, timeout=timeout
            )
        except EmailRejected as rejected:
            return DispatchResult(
                DispatchKind.NOT_APPLIED, "provider_rejected",
                rejected.reference,
            )
        except EmailServerError:
            return DispatchResult(
                DispatchKind.AMBIGUOUS, "ambiguous_error_after_send"
            )
        except EmailRateLimited:
            return DispatchResult(
                DispatchKind.AMBIGUOUS, "ambiguous_rate_limited"
            )
        except NO_RESPONSE_ERRORS:
            return DispatchResult(
                DispatchKind.AMBIGUOUS, "ambiguous_no_response"
            )

        return DispatchResult(
            DispatchKind.APPLIED, "provider_confirmed", delivery_id
        )

    def lookup(self, target, profile):
        # The contract declares no lookup; nothing is asked.
        return normalize_lookup(
            profile, target, RawLookup("unavailable", str(target.operation_id))
        )
