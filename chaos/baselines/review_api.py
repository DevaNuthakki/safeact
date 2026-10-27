"""Local B3 human-review HTTP boundary; no SafeAct protected API functions."""

import logging
from typing import Literal
from uuid import UUID

from fastapi import FastAPI, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from chaos.baselines.identity import Forbidden, Missing, Unauthenticated
from chaos.baselines.review import Review


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid",strict=True)


class Login(Body):
    username: str = Field(min_length=1,max_length=128)
    password: SecretStr = Field(min_length=1,max_length=72)


class Proposal(Body):
    action: Literal["PROTECTED_RETRY","REVERIFY"]
    ttl_seconds: float = Field(default=120,gt=0,le=600)


class Decision(Body):
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_key: str = Field(min_length=1,max_length=128,pattern=r"^\S+$")
    approved: bool


def create_app(runtime):
    app = FastAPI(title="Local B3 human review",docs_url=None,redoc_url=None,openapi_url=None)
    review = Review(runtime)

    @app.exception_handler(RequestValidationError)
    async def invalid(request,error):
        return JSONResponse(status_code=422,content=dict(error="invalid_request"))

    @app.exception_handler(Unauthenticated)
    async def unauthenticated(request,error):
        return JSONResponse(status_code=401,content=dict(error="human_session_required"))

    @app.exception_handler(Forbidden)
    async def forbidden(request,error):
        return JSONResponse(status_code=403,content=dict(error="human_role_or_independence_required"))

    @app.exception_handler(Missing)
    async def missing(request,error):
        return JSONResponse(status_code=404,content=dict(error="not_found"))

    @app.middleware("http")
    async def boundary(request,call_next):
        # Bodies/headers are never logged. Keep validation and internal errors
        # from exposing password/token input, SQL or private configuration.
        try:response = await call_next(request)
        except Exception as error:
            logging.getLogger(__name__).error("BASELINE_API_ERROR:%s",type(error).__name__)
            response = JSONResponse(status_code=503,content=dict(error="temporarily_unavailable"))
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def token(header):
        if header is None or not header.startswith("Bearer "):
            raise Unauthenticated("A human session is required")
        return header[7:]

    def outcome(result):
        if result["status"] not in ("proposed","accepted","replayed"):
            return JSONResponse(status_code=409,content={k:str(v) for k,v in result.items()})
        return result

    @app.get("/healthz")
    def health():return dict(status="ok")

    @app.post("/login")
    def login(body:Login):
        return review.identity.login(body.username,body.password.get_secret_value())

    @app.post("/logout")
    def logout(authorization:str|None=Header(default=None)):
        review.identity.logout(token(authorization))
        return dict(status="revoked")

    @app.get("/actions/{operation_id}/review")
    def snapshot(operation_id:UUID,authorization:str|None=Header(default=None)):
        return review.snapshot(token(authorization),operation_id)

    @app.post("/actions/{operation_id}/proposals")
    def propose(operation_id:UUID,body:Proposal,authorization:str|None=Header(default=None)):
        return outcome(review.propose(token(authorization),operation_id,body.action,ttl_seconds=body.ttl_seconds))

    @app.get('/actions/{operation_id}/proposals/{proposal_id}')
    def proposal(operation_id:UUID,proposal_id:UUID,authorization:str|None=Header(default=None)):
        return review.proposal(token(authorization),operation_id,proposal_id)

    @app.post("/actions/{operation_id}/proposals/{proposal_id}/decisions")
    def decide(operation_id:UUID,proposal_id:UUID,body:Decision,authorization:str|None=Header(default=None)):
        return outcome(review.decide(token(authorization),operation_id,proposal_id,
            body.binding_sha256,body.decision_key,body.approved))
    return app
