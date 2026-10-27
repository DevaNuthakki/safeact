"""Stripe sandbox refunds connector (raw HTTPS, API v1, test mode only).

Dispatch: ``POST /v1/refunds`` (form-encoded) with
- ``Idempotency-Key``: the server-derived ``sa1_`` key (core/provider_keys);
- ``Stripe-Version``: pinned, never the account default;
- ``Authorization: Bearer <restricted rk_test_ key>``;
- ``payment_intent``, ``amount`` and ``metadata[safeact_operation_id]`` /
  ``metadata[safeact_payload_sha256]``, so a lookup can find this
  operation's refund and check it holds this operation's parameters.

No SDK, no client retries, no redirects; the timeout is the remaining
request deadline. Classification (docs.stripe.com/error-low-level,
/api/idempotent_requests, /rate-limits, read 2026-10-20):

  200                                  APPLIED (the refund object exists;
                                       its status is recorded as evidence
                                       detail; a later asynchronous failure
                                       is outside what SafeAct verifies)
  401, 403                             NOT_APPLIED: authentication and
                                       permission fail before execution
  400 idempotency_error                key reused with other parameters
                                       (ambiguous_key_conflict)
  400/402 with a pre-execution code    NOT_APPLIED (validation before
                                       execution: no object created)
  400/402 charge_already_refunded,     NOT_APPLIED: Stripe raises it
          amount_too_large             instead of creating the refund
  409                                  the same key is still executing
                                       (provider_in_flight_conflict)
  429 with Stripe-Rate-Limited-Reason, NOT_APPLIED, retryable: Stripe does
      or code lock_timeout             not process rate-limited or
                                       lock-timed-out requests
  any other status, 5xx, malformed     AMBIGUOUS (a 500 may follow an
                                       applied effect)
  timeout, reset, broken connection    AMBIGUOUS (no response)

Lookup: lists refunds created since the first dispatch (minus a margin)
and matches ``metadata[safeact_operation_id]``. Listing is not documented
as strongly consistent and a refund may still be executing, so the
contract is EVENTUAL: absence is never proof of non-application.

Any object whose ``livemode`` is true stops the run (LiveModeRefused).
The test ledger plays no part: Stripe's own records are the provider
side, and the sandbox runner reads them separately.
"""

import json

from datetime import timedelta

import httpx2 as httpx

from connectors.base import (
    DispatchKind,
    DispatchResult,
    LookupResult,
    RawLookup,
    normalize_lookup,
)
from connectors.stripe_key import StripeKey, require_sandbox_allowed
from core.connector_profiles import STRIPE, STRIPE_API_VERSION
from core.contracts import EvidenceKind


API_BASE = "https://api.stripe.com"
ACTIONS = frozenset({"refund_payment"})
LOOKUP_TIMEOUT = 10.0
LOOKUP_MARGIN = timedelta(minutes=5)
LOOKUP_PAGES = 10

# Error codes Stripe returns from validation before an endpoint executes
# (nothing is created): docs.stripe.com/error-codes.
PRE_EXECUTION_CODES = frozenset({
    "parameter_missing", "parameter_invalid_empty",
    "parameter_invalid_integer", "parameter_invalid_string_empty",
    "parameter_unknown", "resource_missing", "amount_too_small",
})
# Stripe refuses to create the refund for these (docs.stripe.com/api/
# refunds/create): nothing of this request was applied.
REFUSED_CODES = frozenset({"charge_already_refunded", "amount_too_large"})


class LiveModeRefused(RuntimeError):
    """Stripe answered with a live-mode object: stop everything."""


def _live(body):
    return isinstance(body, dict) and body.get("livemode") is True


class StripePaymentsConnector:

    connector_id = STRIPE

    def __init__(self, key, *, transport=None, base_url=API_BASE):
        if not isinstance(key, StripeKey):
            raise TypeError("A StripeKey is required")
        if transport is None:
            # Real network: only the real API, only when allowed.
            require_sandbox_allowed()
            if base_url != API_BASE:
                raise ValueError("Real calls go to api.stripe.com only")
        self._http = httpx.Client(
            base_url=base_url, transport=transport, follow_redirects=False,
            trust_env=False, headers={
                "Authorization": f"Bearer {key.reveal()}",
                "Stripe-Version": STRIPE_API_VERSION,
            },
        )

    def __repr__(self):
        return "StripePaymentsConnector(<key hidden>)"

    def accepts(self, intent):
        return (
            intent.connector_id == STRIPE
            and intent.action_type in ACTIONS
            and bool(intent.provider_idempotency_key)
        )

    # -- dispatch -------------------------------------------------------

    def _form(self, intent, operation_id):
        params = json.loads(intent.payload_canonical)
        intent_id, amount = params.get("payment_intent"), params.get(
            "amount_minor")
        if (type(intent_id) is not str or not intent_id.startswith("pi_")
                or type(amount) is not int or amount <= 0):
            raise ValueError("payload needs payment_intent and amount_minor")
        return {
            "payment_intent": intent_id,
            "amount": str(amount),
            "metadata[safeact_operation_id]": str(operation_id),
            "metadata[safeact_payload_sha256]": intent.payload_sha256,
        }

    def dispatch(self, intent, *, operation_id, timeout=None):
        try:
            form = self._form(intent, operation_id)
        except (ValueError, TypeError, AttributeError):
            # Refused locally: nothing was sent, so nothing can apply.
            return DispatchResult(DispatchKind.NOT_APPLIED,
                                  "provider_rejected", "local:invalid_payload")
        try:
            response = self._http.post(
                "/v1/refunds", data=form,
                headers={"Idempotency-Key": intent.provider_idempotency_key},
                timeout=httpx.Timeout(timeout or 30.0),
            )
        except httpx.TimeoutException:
            return DispatchResult(DispatchKind.AMBIGUOUS,
                                  "ambiguous_no_response")
        except httpx.TransportError:
            return DispatchResult(DispatchKind.AMBIGUOUS,
                                  "ambiguous_no_response")
        return classify(response)

    # -- lookup ---------------------------------------------------------

    def lookup(self, target, profile):
        if target.first_dispatch_at is None:
            return normalize_lookup(profile, target,
                                    RawLookup("unavailable", ""))
        since = int((target.first_dispatch_at - LOOKUP_MARGIN).timestamp())
        operation = str(target.operation_id)
        params = {"created[gte]": str(since), "limit": "100"}
        found = []
        try:
            for _ in range(LOOKUP_PAGES):
                response = self._http.get(
                    "/v1/refunds", params=params,
                    timeout=httpx.Timeout(LOOKUP_TIMEOUT),
                )
                if response.status_code != 200:
                    return normalize_lookup(profile, target, RawLookup(
                        "unavailable", operation))
                body = response.json()
                data = body.get("data") if isinstance(body, dict) else None
                if not isinstance(data, list):
                    raise ValueError("refund list without data")
                for refund in data:
                    if _live(refund):
                        raise LiveModeRefused("live-mode refund in a list")
                    metadata = (refund or {}).get("metadata") or {}
                    if metadata.get("safeact_operation_id") == operation:
                        found.append(refund)
                if not body.get("has_more"):
                    break
                params["starting_after"] = data[-1]["id"]
            else:
                # More refunds in the window than this lookup reads.
                return normalize_lookup(profile, target, RawLookup(
                    "unavailable", operation))
        except LiveModeRefused:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError,
                AttributeError):
            return normalize_lookup(profile, target, RawLookup(
                "unavailable", operation))

        if not found:
            return normalize_lookup(profile, target, RawLookup(
                "absent", operation))
        refund = found[-1]  # oldest of the matches (the list is newest first)
        if type(refund.get("id")) is not str:
            return LookupResult(EvidenceKind.INCONCLUSIVE,
                                note="out_of_contract_refund")
        return normalize_lookup(profile, target, RawLookup(
            "found", operation, refund["id"],
            (refund.get("metadata") or {}).get("safeact_payload_sha256"),
            provider_facts=_status(refund),
        ))


def _status(refund):
    """The refund's status as Stripe reported it, recorded as evidence.

    pending, requires_action, succeeded, failed or canceled. SafeAct
    verifies that the refund was created; a refund that later fails
    asynchronously is outside what it verifies (no webhooks).
    """
    status = refund.get("status")
    if type(status) is str and 0 < len(status) <= 32 and status.replace(
            "_", "").isalpha():
        return {"refund_status": status}
    return {"refund_status": "unreported"}


def _error(response):
    try:
        body = response.json()
    except ValueError:
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def _reference(response, code):
    request_id = response.headers.get("request-id") or "no-request-id"
    return f"stripe:{response.status_code}:{code or 'none'}:{request_id}"


def classify(response):
    status = response.status_code
    if status == 200:
        try:
            body = response.json()
        except ValueError:
            body = None
        if _live(body):
            raise LiveModeRefused("live-mode refund")
        if (isinstance(body, dict) and body.get("object") == "refund"
                and type(body.get("id")) is str and body["id"]):
            return DispatchResult(DispatchKind.APPLIED, "provider_confirmed",
                                  body["id"], provider_facts=_status(body))
        return DispatchResult(DispatchKind.AMBIGUOUS,
                              "ambiguous_error_after_send")

    error = _error(response)
    code = error.get("code") if type(error.get("code")) is str else None
    kind = error.get("type") if type(error.get("type")) is str else None

    if status in (401, 403):
        return DispatchResult(DispatchKind.NOT_APPLIED, "provider_rejected",
                              _reference(response, code or kind))
    if status == 400 and kind == "idempotency_error":
        return DispatchResult(DispatchKind.AMBIGUOUS, "ambiguous_key_conflict",
                              observed=EvidenceKind.INCONCLUSIVE)
    if status in (400, 402) and code in REFUSED_CODES | PRE_EXECUTION_CODES:
        return DispatchResult(DispatchKind.NOT_APPLIED, "provider_rejected",
                              _reference(response, code))
    if status == 409:
        return DispatchResult(DispatchKind.AMBIGUOUS,
                              "provider_in_flight_conflict",
                              observed=EvidenceKind.INCONCLUSIVE)
    if status == 429 and (response.headers.get("stripe-rate-limited-reason")
                          or code == "lock_timeout"):
        facts = {"rate_limited": "true"}
        retry_after = response.headers.get("retry-after", "")
        if retry_after.isdigit() and int(retry_after) <= 86400:
            facts["retry_after"] = f"{int(retry_after)}.000"
        return DispatchResult(DispatchKind.NOT_APPLIED, "provider_rejected",
                              _reference(response, code or "rate_limited"),
                              retryable=True, provider_facts=facts)
    return DispatchResult(DispatchKind.AMBIGUOUS, "ambiguous_error_after_send")
