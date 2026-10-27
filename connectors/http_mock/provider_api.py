"""Wire APIs for the mock providers (test doubles, never providers).

One FastAPI app per mock, serving the mock's provider surface over HTTP
so a real network path (and Toxiproxy) can sit between SafeAct and the
provider. Answers come only from the mock's own provider-side state;
this module never reads the harness's effects ledger (structural test).

Wire contract (what connectors/http_providers.py expects):

payments  POST /v1/refunds, /v1/charges   header Idempotency-Key, body = params
            200 {"id", "replayed"}                    applied (or replayed)
            429 {"error": {"type": "rate_limited"}}   + Retry-After (all three)
            402 {"error": {"type": "rejected", "reference", "retryable"}}
            400 {"error": {"type": "idempotency_error"}}  same key, other params
            409 {"error": {"type": "key_in_use"}}     first request in flight
            502                                       server error (effect unknown)
          GET  /v1/lookup?idempotency_key=           200 {"status", "id", "params"}
            503                                       lookup unavailable
crm       POST /records {"external_ref", "params"}   201 {"record_id", ...}
            422 {"error": {"type": "validation", "reference", "retryable"}}
            503                                       server error
          GET  /records?external_ref=                200 {"records": [...]}; 503
email     POST /messages {"message_id", "params"}    202 {"delivery_id"}
            422 {"error": {"type": "rejected", "reference"}}; 500

"No response" faults (the mock raising its timeout) are served as a
stall: the server holds the connection until the client gives up, so
the caller sees a real timeout. A stalled request ends with 504 if the
client is still there after ``stall_seconds``.
"""

import threading

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from connectors.crm_mock import (
    CrmRateLimited,
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
    ProviderRateLimited,
    ProviderRejected,
    ProviderServerError,
    ProviderTimeout,
)


class Stall:
    """Hold a 'no response' request open until the client times out."""

    def __init__(self, seconds=120.0):
        self.seconds = seconds
        self._released = threading.Event()

    def __call__(self):
        self._released.wait(self.seconds)
        return JSONResponse({"error": {"type": "stalled"}}, status_code=504)

    def release_all(self):
        """Server shutdown: end every stalled request."""
        self._released.set()


def _error(status, **error):
    return JSONResponse({"error": error}, status_code=status)


def _rate_limited(limited):
    headers = {}
    if limited.retry_after is not None:
        headers["Retry-After"] = f"{float(limited.retry_after):g}"
    return JSONResponse({"error": {"type": "rate_limited"}}, status_code=429,
                        headers=headers)


async def _body(request):
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def payments_app(provider, stall):
    app = FastAPI(title="mock-payments", openapi_url=None, docs_url=None,
                  redoc_url=None)

    def request(method, key, params):
        if not key or not isinstance(params, dict):
            return _error(400, type="invalid_request")
        try:
            receipt = method(key, params)
        except ProviderRejected as rejected:
            return _error(402, type="rejected", reference=rejected.reference,
                          retryable=rejected.retryable)
        except IdempotencyConflict:
            return _error(400, type="idempotency_error")
        except ProviderInFlight:
            return _error(409, type="key_in_use")
        except ProviderRateLimited as limited:
            return _rate_limited(limited)
        except ProviderServerError:
            return _error(502, type="server_error")
        except ProviderTimeout:
            return stall()
        return JSONResponse({"id": receipt.refund_id,
                             "replayed": receipt.replayed})

    @app.post("/v1/refunds")
    def refund(params: dict, idempotency_key: str = Header(default="")):
        return request(provider.refund, idempotency_key, params)

    @app.post("/v1/charges")
    def charge(params: dict, idempotency_key: str = Header(default="")):
        return request(provider.charge, idempotency_key, params)

    @app.get("/v1/lookup")
    def lookup(idempotency_key: str):
        try:
            found = provider.lookup(idempotency_key)
        except LookupUnavailable:
            return _error(503, type="lookup_unavailable")
        return JSONResponse({"status": found.status, "id": found.refund_id,
                             "params": found.params})

    return app


def crm_app(provider, stall):
    app = FastAPI(title="mock-crm", openapi_url=None, docs_url=None,
                  redoc_url=None)

    @app.post("/records", status_code=201)
    async def create(request: Request):
        body = await _body(request)
        if body is None or not isinstance(body.get("params"), dict):
            return _error(400, type="invalid_request")
        return await _threaded(lambda: _create(body))

    def _create(body):
        try:
            record = provider.create_record(body.get("external_ref"),
                                            body["params"])
        except CrmValidationRejected as rejected:
            return _error(422, type="validation",
                          reference=rejected.reference,
                          retryable=rejected.retryable)
        except CrmServerError:
            return _error(503, type="server_error")
        except CrmRateLimited as limited:
            return _rate_limited(limited)
        except CrmTimeout:
            return stall()
        except (ValueError, TypeError):
            return _error(400, type="invalid_request")
        return JSONResponse(_record(record), status_code=201)

    @app.get("/records")
    def search(external_ref: str):
        try:
            found = provider.search(external_ref)
        except CrmSearchUnavailable:
            return _error(503, type="search_unavailable")
        return JSONResponse({"records": [_record(r) for r in found]})

    return app


def _record(record):
    return {"record_id": record.record_id,
            "external_ref": record.external_ref, "params": record.params}


def email_app(provider, stall):
    app = FastAPI(title="mock-email", openapi_url=None, docs_url=None,
                  redoc_url=None)

    @app.post("/messages", status_code=202)
    async def send(request: Request):
        body = await _body(request)
        if body is None:
            return _error(400, type="invalid_request")
        return await _threaded(lambda: _send(body))

    def _send(body):
        try:
            delivery = provider.send(body.get("message_id"),
                                     body.get("params") or {})
        except EmailRejected as rejected:
            return _error(422, type="rejected", reference=rejected.reference)
        except EmailServerError:
            return _error(500, type="server_error")
        except EmailRateLimited as limited:
            return _rate_limited(limited)
        except EmailTimeout:
            return stall()
        except (ValueError, TypeError):
            return _error(400, type="invalid_request")
        return JSONResponse({"delivery_id": delivery}, status_code=202)

    return app


async def _threaded(function):
    # The mocks block (held calls, stalls); keep the event loop free.
    return await run_in_threadpool(function)
