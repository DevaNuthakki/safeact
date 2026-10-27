"""Propose a next action; review it with a version-bound decision.

An investigator proposes one permitted next action. A reviewer sees
the proposal with its binding (operation version, payload hash,
evidence digest, contract, action, expiry) and approves or rejects it
by echoing the binding digest their screen showed. PostgreSQL rejects
a stale or expired binding, a second decision, and anyone deciding on
their own proposal or request. The approval is the only authority an
approved action needs from a person; every safety rule still applies.
"""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api.actions import csrf, no_duplicate_keys, schema, secret
from api.db import transaction
from api.errors import error_body
from storage import product_api as product


router = APIRouter(tags=["review"])

ACTIONS = Literal["REVERIFY", "PROTECTED_RETRY", "ATTEST_APPLIED"]
STATUSES = Literal["open", "stale", "expired", "approved", "rejected", "all"]


class ProposeAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ACTIONS
    rationale: str = Field(min_length=1, max_length=2000)
    attested_reference: str | None = Field(
        None, min_length=1, max_length=255,
        description="ATTEST_APPLIED only: the provider's reference for "
                    "the applied effect. Never proof of non-application.",
    )


class Decide(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_key: UUID = Field(
        description="Client-generated once per decision; a repeated "
                    "click with the same key returns the stored decision.",
    )
    note: str | None = Field(None, max_length=2000)


@router.post(
    "/actions/{operation_id}/proposals", status_code=201,
    dependencies=[Depends(no_duplicate_keys)],
)
def propose(operation_id: UUID, body: ProposeAction, request: Request):
    with transaction(request) as connection:
        return product.propose(
            connection, schema(request),
            secret=secret(request), csrf=csrf(request),
            operation_id=operation_id, action=body.action,
            rationale=body.rationale,
            attested_reference=body.attested_reference,
        )


@router.post("/actions/{operation_id}/verification-requests", status_code=201)
def request_verification(operation_id: UUID, request: Request):
    """Ask for one more lookup now (investigator, no approval needed).

    Lookup-only and inside the contract's bounds: after the check
    interval since the last lookup, before the post-escalation horizon,
    a few per escalation. Beyond them, propose REVERIFY for a reviewer.
    """

    with transaction(request) as connection:
        return product.request_verification(
            connection, schema(request),
            secret=secret(request), csrf=csrf(request),
            operation_id=operation_id,
        )


@router.get("/proposals")
def list_proposals(
    request: Request,
    status: STATUSES = "open",
    limit: int = Query(50, ge=1, le=100),
):
    with transaction(request) as connection:
        return {"items": product.proposals(
            connection, schema(request), secret=secret(request),
            status=status, limit=limit,
        )}


@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: UUID, request: Request):
    with transaction(request) as connection:
        return product.proposal(
            connection, schema(request), secret=secret(request),
            proposal_id=proposal_id,
        )


@router.post(
    "/proposals/{proposal_id}/decision",
    dependencies=[Depends(no_duplicate_keys)],
    responses={409: {"description": "Stale approval or already decided."}},
)
def decide(proposal_id: UUID, body: Decide, request: Request):
    with transaction(request) as connection:
        result = product.decide(
            connection, schema(request),
            secret=secret(request), csrf=csrf(request),
            proposal_id=proposal_id, decision=body.decision,
            binding_digest=body.binding_digest,
            decision_key=body.decision_key, note=body.note,
        )
    # The refusal row is committed by now; tell the reviewer what changed.
    if result["outcome"] == "refused_stale":
        body = error_body(
            "stale_approval",
            "The operation or its evidence changed since this proposal was "
            "shown. Re-read it before deciding.",
        )
        body["error"]["reason"] = result["reason"]
        body["error"]["proposal"] = result["proposal"]
        return JSONResponse(body, status_code=409)
    return result
