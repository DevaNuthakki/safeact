"""Map refusals to HTTP responses without leaking internals.

Protected SQL functions refuse by SQLSTATE; ``storage.durable_lifecycle``
already turns each SQLSTATE into a typed ``LifecycleRejected``. Here
each type becomes a stable error code and status. Responses carry the
code and a fixed message only: never SQL text, stack traces, or the
submitted input (a validation error must not echo a password back).
"""

import logging

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.cookies import clear_session_cookies
from storage import durable_lifecycle as lifecycle
from storage import product_api as product


log = logging.getLogger("safeact.api")


class ApiError(Exception):
    """A deliberate refusal with a stable code."""

    def __init__(self, status, code, message, *, body=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.body = body

    @classmethod
    def fixed(cls, status, body):
        """A refusal whose exact body is part of the contract."""

        error = body["error"]
        return cls(status, error["code"], error["message"], body=body)


PRODUCT_ERRORS = {
    product.Unauthenticated: (401, "unauthenticated",
                              "Sign in to continue."),
    product.CsrfRefused: (403, "csrf_refused",
                          "The request is missing a valid CSRF token."),
    product.RoleRequired: (403, "forbidden",
                           "Your role does not allow this action."),
    product.IdentityInvalid: (409, "identity_invalid",
                              "The identity change was refused."),
    product.ActionNotGranted: (403, "not_granted",
                               "You are not permitted to request this "
                               "action on this resource."),
    product.RequestConflict: (409, "request_conflict",
                              "This request key was already used for a "
                              "different request."),
    product.RequestInvalid: (422, "invalid_request",
                             "The request is not valid."),
    product.AlreadyDecided: (409, "already_decided",
                             "This proposal was already decided."),
    product.SelfReview: (403, "self_review",
                         "You cannot decide on a proposal you made or an "
                         "action you requested."),
    product.NotEligible: (409, "not_eligible",
                          "That is not a permitted next action for this "
                          "operation now."),
}


# Most specific first; the first isinstance match wins.
LIFECYCLE_ERRORS = (
    (lifecycle.UnknownOperation, 404, "not_found", "Not found."),
    (lifecycle.StaleTransition, 409, "stale_version",
     "The operation changed; re-read it and try again."),
    (lifecycle.IllegalTransition, 409, "illegal_transition",
     "That change is not allowed from the current state."),
    (lifecycle.ContractUnknown, 409, "contract_unknown",
     "No registered connector contract for this operation."),
    (lifecycle.LifecycleRejected, 409, "precondition_failed",
     "The request was refused by a safety rule."),
)


def error_body(code, message, request_id=None):
    body = {"error": {"code": code, "message": message}}
    if request_id:
        body["error"]["request_id"] = request_id
    return body


def _request_id(request):
    return getattr(request.state, "request_id", None)


def lifecycle_status(error):
    for kind, status, code, message in LIFECYCLE_ERRORS:
        if isinstance(error, kind):
            return status, code, message
    raise TypeError("not a lifecycle refusal")


def _sanitized(errors):
    # Pydantic includes the offending input and context; keep only
    # where the problem is and what kind it is.
    return [
        {"loc": list(e.get("loc", ())), "type": e.get("type", "invalid")}
        for e in errors
    ]


def install(app: FastAPI):

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError):
        return JSONResponse(
            error.body if error.body is not None
            else error_body(error.code, error.message, _request_id(request)),
            status_code=error.status,
        )

    @app.exception_handler(product.ProductRefused)
    async def product_refused(request: Request, error):
        status, code, message = PRODUCT_ERRORS[type(error)]
        log.info("refused", extra={
            "request_id": _request_id(request),
            "refusal": type(error).__name__,
        })
        response = JSONResponse(
            error_body(code, message, _request_id(request)),
            status_code=status,
        )
        if status == 401:
            # A dead session: drop the browser's copies too.
            clear_session_cookies(response)
        return response

    @app.exception_handler(lifecycle.LifecycleRejected)
    async def lifecycle_refused(request: Request, error):
        status, code, message = lifecycle_status(error)
        log.info("refused", extra={
            "request_id": _request_id(request),
            "refusal": type(error).__name__,
        })
        return JSONResponse(
            error_body(code, message, _request_id(request)),
            status_code=status,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, error: RequestValidationError):
        body = error_body(
            "invalid_request", "The request is not valid.",
            _request_id(request),
        )
        body["error"]["fields"] = _sanitized(error.errors())
        return JSONResponse(body, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(
            error.status_code, "http_error"
        )
        message = {404: "Not found.", 405: "Method not allowed."}.get(
            error.status_code, "Request failed."
        )
        return JSONResponse(
            error_body(code, message, _request_id(request)),
            status_code=error.status_code,
        )

    @app.exception_handler(psycopg.OperationalError)
    async def database_unavailable(request: Request, error):
        log.warning("database_unavailable", extra={
            "request_id": _request_id(request),
            "error_type": type(error).__name__,
        })
        return JSONResponse(
            error_body(
                "unavailable", "The service is temporarily unavailable.",
                _request_id(request),
            ),
            status_code=503,
        )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception):
        log.error("unhandled_error", extra={
            "request_id": _request_id(request),
            "error_type": type(error).__name__,
        })
        return JSONResponse(
            error_body(
                "internal_error", "Unexpected server error.",
                _request_id(request),
            ),
            status_code=500,
        )
