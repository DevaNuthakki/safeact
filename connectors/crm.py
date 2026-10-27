"""CRM connector adapter over the mock CRM provider.

No native idempotency: the operation id is sent as ``external_ref`` for
correlation only. Search is eventually consistent and may be
incomplete, so absence never proves non-application; a found record
counts only if it holds exactly this operation's parameters.
"""

import json

from core.connector_profiles import CRM
from core.contracts import canonical_payload_hash
from connectors.base import (
    DispatchKind,
    DispatchResult,
    RawLookup,
    normalize_lookup,
)
from connectors.crm_mock import (
    CrmRateLimited,
    CrmSearchUnavailable,
    CrmServerError,
    CrmTimeout,
    CrmValidationRejected,
)


ACTIONS = frozenset({"create_replacement_order"})

NO_RESPONSE_ERRORS = (CrmTimeout, ConnectionError, OSError)


class CrmConnector:

    connector_id = CRM

    def __init__(self, provider):
        self._provider = provider

    def accepts(self, intent):
        return intent.connector_id == CRM and intent.action_type in ACTIONS

    def dispatch(self, intent, *, operation_id, timeout=None):
        params = json.loads(intent.payload_canonical)

        try:
            record = self._provider.create_record(
                str(operation_id), params, timeout=timeout
            )
        except CrmValidationRejected as rejected:
            return DispatchResult(
                DispatchKind.NOT_APPLIED, "provider_rejected",
                rejected.reference, retryable=rejected.retryable,
            )
        except CrmServerError:
            return DispatchResult(
                DispatchKind.AMBIGUOUS, "ambiguous_error_after_send"
            )
        except CrmRateLimited:
            # Not documented as pre-execution: the record may exist.
            return DispatchResult(
                DispatchKind.AMBIGUOUS, "ambiguous_rate_limited"
            )
        except NO_RESPONSE_ERRORS:
            return DispatchResult(
                DispatchKind.AMBIGUOUS, "ambiguous_no_response"
            )

        return DispatchResult(
            DispatchKind.APPLIED, "provider_confirmed", record.record_id
        )

    def lookup(self, target, profile):
        ref = str(target.operation_id)
        try:
            records = self._provider.search(ref)
        except CrmSearchUnavailable:
            raw = RawLookup("unavailable", ref)
        else:
            if not records:
                raw = RawLookup("absent", ref)
            else:
                bound = [
                    r for r in records
                    if canonical_payload_hash(r.params)
                    == target.payload_sha256
                ]
                chosen = bound[0] if bound else records[0]
                raw = RawLookup(
                    "found", ref, chosen.record_id,
                    canonical_payload_hash(chosen.params),
                )
        return normalize_lookup(profile, target, raw)
