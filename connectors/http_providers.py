"""HTTP clients for the mock providers' wire APIs.

Each client has the same surface as the matching in-process mock
(``refund``/``charge``/``lookup``, ``create_record``/``search``, ``send``)
and raises the same exceptions. So PaymentsConnector, CrmConnector and
EmailConnector classify a real HTTP exchange with their unchanged rules,
and Toxiproxy can sit on the wire between them and the provider.

Mapping (the provider's documented wire contract, connectors/http_mock):
- a decisive provider answer maps to its exception: rejection,
  idempotency conflict, key in use, lookup or search unavailable;
- any 5xx is a server error: it says nothing about the effect (F03);
- a timeout, a reset or a broken connection is "no response": the effect
  may or may not have been applied. Even a refused connection is not
  treated as proof here; that classification is the connector
  contract's, and it stays conservative;
- an answer outside the wire contract (unknown status, malformed body)
  after the request was sent says nothing about the effect, so it is
  reported as a server error (ambiguous); a malformed lookup or search
  answer is reported as unavailable (inconclusive). Neither is ever
  decisive.

No client retries, no redirects, explicit timeouts from the remaining
request deadline. Never talks to the harness admin port.
"""

import httpx2 as httpx

from connectors.crm_mock import (
    CrmRateLimited,
    CrmRecord,
    CrmSearchUnavailable,
    CrmServerError,
    CrmTimeout,
    CrmValidationRejected,
)
from connectors.email_mock import (
    EmailRateLimited,
    EmailRejected,
    EmailServerError,
    EmailTimeout,
)
from connectors.payments_mock import (
    IdempotencyConflict,
    LookupUnavailable,
    ProviderInFlight,
    ProviderLookup,
    ProviderRateLimited,
    ProviderRejected,
    ProviderServerError,
    ProviderTimeout,
    RefundReceipt,
)


# Lookups are not bounded by a dispatch deadline; keep them short.
LOOKUP_TIMEOUT = 5.0
MAX_TIMEOUT = 300.0


class _OffContract(Exception):
    """An answer outside the wire contract (mapped by each caller)."""


def _timeout(seconds):
    if seconds is None:
        return httpx.Timeout(MAX_TIMEOUT)
    seconds = float(seconds)
    if not 0 < seconds <= MAX_TIMEOUT:
        # A spent deadline: the worker never sends after it (the
        # connector is only called with time left).
        seconds = min(max(seconds, 0.001), MAX_TIMEOUT)
    return httpx.Timeout(seconds)


def _client(base_url, client, *, provider, observer):
    if client is None:
        client = httpx.Client(
            base_url=base_url, follow_redirects=False, trust_env=False,
        )
    if observer is not None:
        from connectors.http_trace import ObservedClient
        client = ObservedClient(provider, client, observer)
    return client


def _error(response):
    try:
        body = response.json()
    except ValueError:
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def _retry_after(response):
    value = response.headers.get("retry-after")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if 0 <= seconds <= 86400 else None


def _json(response):
    try:
        body = response.json()
    except ValueError as error:
        raise _OffContract("body is not JSON") from error
    if not isinstance(body, dict):
        raise _OffContract("body is not an object")
    return body


class HttpPaymentsProvider:

    def __init__(self, base_url, *, client=None, observer=None):
        self._http = _client(base_url, client, provider="payments", observer=observer)

    def refund(self, idempotency_key, params, *, timeout=None):
        return self._request("/v1/refunds", idempotency_key, params, timeout)

    def charge(self, idempotency_key, params, *, timeout=None):
        return self._request("/v1/charges", idempotency_key, params, timeout)

    def _request(self, path, idempotency_key, params, timeout):
        if type(idempotency_key) is not str or not idempotency_key:
            raise ValueError("Idempotency key required")
        if type(params) is not dict:
            raise TypeError("Request parameters must be a dict")
        try:
            response = self._http.post(
                path, json=params, timeout=_timeout(timeout),
                headers={"Idempotency-Key": idempotency_key},
            )
        except httpx.TimeoutException as error:
            raise ProviderTimeout("no response") from error
        except httpx.TransportError as error:
            raise ConnectionError("connection failed") from error

        try:
            return self._answer(response, idempotency_key)
        except _OffContract as error:
            raise ProviderServerError(f"off contract: {error}") from error

    def _answer(self, response, idempotency_key):
        if response.status_code == 200:
            body = _json(response)
            if type(body.get("id")) is not str or not body["id"]:
                raise _OffContract("receipt without id")
            return RefundReceipt(
                body["id"], idempotency_key, bool(body.get("replayed")),
            )
        error = _error(response)
        if response.status_code == 402 and error.get("type") == "rejected":
            reference = error.get("reference")
            if type(reference) is not str or not reference:
                raise _OffContract("rejection without reference")
            raise ProviderRejected(
                reference, retryable=error.get("retryable") is True,
            )
        if (response.status_code == 400
                and error.get("type") == "idempotency_error"):
            raise IdempotencyConflict(idempotency_key)
        if response.status_code == 409 and error.get("type") == "key_in_use":
            raise ProviderInFlight(idempotency_key)
        if response.status_code == 429:
            raise ProviderRateLimited(_retry_after(response))
        if response.status_code >= 500:
            raise ProviderServerError(str(response.status_code))
        raise _OffContract(str(response.status_code))

    def lookup(self, idempotency_key):
        try:
            response = self._http.get(
                "/v1/lookup", params={"idempotency_key": idempotency_key},
                timeout=_timeout(LOOKUP_TIMEOUT),
            )
        except httpx.HTTPError as error:
            raise LookupUnavailable(idempotency_key) from error
        if response.status_code != 200:
            raise LookupUnavailable(idempotency_key)
        try:
            body = _json(response)
        except _OffContract as error:
            raise LookupUnavailable(idempotency_key) from error
        status, found_id, params = (
            body.get("status"), body.get("id"), body.get("params"),
        )
        if status not in ("found", "absent", "in_progress") or (
            status == "found" and (type(found_id) is not str
                                   or not isinstance(params, dict))
        ):
            raise LookupUnavailable(idempotency_key)
        return ProviderLookup(
            status, found_id if status == "found" else None,
            dict(params) if status == "found" else None,
        )


class HttpCrmProvider:

    def __init__(self, base_url, *, client=None, observer=None):
        self._http = _client(base_url, client, provider="crm", observer=observer)

    def create_record(self, external_ref, params, *, timeout=None):
        if type(external_ref) is not str or not external_ref:
            raise ValueError("external_ref required")
        if type(params) is not dict:
            raise TypeError("record parameters must be a dict")
        try:
            response = self._http.post(
                "/records", json={"external_ref": external_ref,
                                  "params": params},
                timeout=_timeout(timeout),
            )
        except httpx.TimeoutException as error:
            raise CrmTimeout("no response") from error
        except httpx.TransportError as error:
            raise ConnectionError("connection failed") from error

        try:
            return self._answer(response)
        except _OffContract as error:
            raise CrmServerError(f"off contract: {error}") from error

    def _answer(self, response):
        if response.status_code == 201:
            return _record(_json(response))
        error = _error(response)
        if response.status_code == 422 and error.get("type") == "validation":
            reference = error.get("reference")
            if type(reference) is not str or not reference:
                raise _OffContract("rejection without reference")
            raise CrmValidationRejected(
                reference, retryable=error.get("retryable") is True,
            )
        if response.status_code == 429:
            raise CrmRateLimited(_retry_after(response))
        if response.status_code >= 500:
            raise CrmServerError(str(response.status_code))
        raise _OffContract(str(response.status_code))

    def search(self, external_ref):
        try:
            response = self._http.get(
                "/records", params={"external_ref": external_ref},
                timeout=_timeout(LOOKUP_TIMEOUT),
            )
        except httpx.HTTPError as error:
            raise CrmSearchUnavailable(external_ref) from error
        if response.status_code != 200:
            raise CrmSearchUnavailable(external_ref)
        try:
            records = _json(response).get("records")
            if not isinstance(records, list):
                raise _OffContract("records is not a list")
            return tuple(_record(r) for r in records)
        except _OffContract as error:
            raise CrmSearchUnavailable(external_ref) from error


def _record(body):
    if not isinstance(body, dict):
        raise _OffContract("record is not an object")
    record_id, ref, params = (
        body.get("record_id"), body.get("external_ref"), body.get("params"),
    )
    if (type(record_id) is not str or type(ref) is not str
            or not isinstance(params, dict)):
        raise _OffContract("malformed record")
    return CrmRecord(record_id, ref, dict(params))


class HttpEmailProvider:

    def __init__(self, base_url, *, client=None, observer=None):
        self._http = _client(base_url, client, provider="email", observer=observer)

    def send(self, message_id, params, *, timeout=None):
        if type(message_id) is not str or not message_id:
            raise ValueError("message id required")
        try:
            response = self._http.post(
                "/messages", json={"message_id": message_id,
                                   "params": params},
                timeout=_timeout(timeout),
            )
        except httpx.TimeoutException as error:
            raise EmailTimeout("no response") from error
        except httpx.TransportError as error:
            raise ConnectionError("connection failed") from error

        try:
            return self._answer(response)
        except _OffContract as error:
            raise EmailServerError(f"off contract: {error}") from error

    def _answer(self, response):
        if response.status_code == 202:
            delivery = _json(response).get("delivery_id")
            if type(delivery) is not str or not delivery:
                raise _OffContract("delivery without id")
            return delivery
        error = _error(response)
        if response.status_code == 422 and error.get("type") == "rejected":
            reference = error.get("reference")
            if type(reference) is not str or not reference:
                raise _OffContract("rejection without reference")
            raise EmailRejected(reference)
        if response.status_code == 429:
            raise EmailRateLimited(_retry_after(response))
        if response.status_code >= 500:
            raise EmailServerError(str(response.status_code))
        raise _OffContract(str(response.status_code))
