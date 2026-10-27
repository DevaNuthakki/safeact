"""Submit, track and cancel actions; create and read workflows.

Every handler forwards the session secret (and, for writes, the CSRF
token) to one protected SQL function. Tenant, actor, visibility and
grants are decided there; nothing here can widen them. A request body
has no tenant, actor or connector field to choose.
"""

import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from api.catalog import CONSOLE_ACTIONS, IDENTIFIER, canonical
from api.cookies import CSRF_HEADER, SESSION_COOKIE
from api.db import transaction
from api.errors import ApiError
from core.contracts import canonical_payload_bytes
from storage import product_api as product


router = APIRouter(tags=["actions"])

REQUEST_KEY = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$"


async def no_duplicate_keys(request: Request):
    """Refuse JSON with a repeated key (the canonical form needs one)."""

    body = await request.body()
    if not body:
        return

    def pairs(items):
        keys = [key for key, _ in items]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate key")
        return dict(items)

    try:
        json.loads(body, object_pairs_hook=pairs)
    except ValueError:
        raise ApiError(
            422, "invalid_request", "The request is not valid."
        ) from None


def secret(request: Request):
    return request.cookies.get(SESSION_COOKIE)


def csrf(request: Request):
    return request.headers.get(CSRF_HEADER)


def schema(request: Request):
    return request.app.state.settings.schema


# -- models ---------------------------------------------------------

class SubmitAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_key: str = Field(
        pattern=REQUEST_KEY,
        description="Client-generated once per form; resubmitting the "
                    "same key returns the same operation.",
    )
    action_type: Literal[tuple(CONSOLE_ACTIONS)]
    provider_account_id: str = Field(pattern=IDENTIFIER)
    contract_version: str = Field(default="v1", pattern=IDENTIFIER)
    payload: dict


class Admitted(BaseModel):
    operation_id: UUID
    created: bool


class OperationSummary(BaseModel):
    operation_id: UUID
    request_key: str
    action_type: str
    resource_id: str
    connector_id: str
    contract_version: str
    state: str
    seq: int
    unknown_reason: str | None
    requested_by: str | None
    created_at: datetime
    updated_at: datetime


class OperationPage(BaseModel):
    items: list[OperationSummary]
    next_before: datetime | None
    next_before_id: UUID | None


class CancelResult(BaseModel):
    outcome: str
    state: str
    seq: int
    undone: bool = Field(
        False,
        description="Always false: a cancellation never undoes a "
                    "remote action that may have been dispatched.",
    )


class GrantView(BaseModel):
    connector_id: str
    provider_account_id: str
    action_type: str
    resource_id: str
    contract_version: str


class CreateWorkflow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_key: str = Field(pattern=REQUEST_KEY)
    customer_id: str = Field(pattern=IDENTIFIER)
    case_id: str = Field(pattern=IDENTIFIER)
    sku: str = Field(pattern=IDENTIFIER)
    fee_minor: int = Field(gt=0, le=10_000_000, strict=True)
    payments_account_id: str = Field(pattern=IDENTIFIER)
    crm_account_id: str = Field(pattern=IDENTIFIER)
    charge_contract: str = Field(default="v1", pattern=IDENTIFIER)
    order_contract: str = Field(default="v1", pattern=IDENTIFIER)
    compensation_contract: str = Field(default="v1", pattern=IDENTIFIER)


class WorkflowAdmitted(BaseModel):
    workflow_id: UUID
    charge_operation_id: UUID
    created: bool


# -- routes ---------------------------------------------------------

@router.get("/grants", response_model=list[GrantView])
def grants(request: Request):
    with transaction(request) as connection:
        rows = product.my_grants(
            connection, schema(request), secret=secret(request)
        )
    return [GrantView(**vars_of(row)) for row in rows]


@router.post(
    "/actions", response_model=Admitted, status_code=201,
    dependencies=[Depends(no_duplicate_keys)],
)
def submit(body: SubmitAction, request: Request, response: Response):
    spec = CONSOLE_ACTIONS[body.action_type]
    try:
        payload = spec.payload.model_validate(body.payload)
    except ValidationError:
        raise ApiError(
            422, "invalid_request", "The payload is not valid for this action."
        ) from None

    with transaction(request) as connection:
        admitted = product.submit(
            connection, schema(request),
            secret=secret(request), csrf=csrf(request),
            request_key=body.request_key,
            action_type=spec.action_type or body.action_type,
            resource_id=getattr(payload, spec.resource_field),
            provider_account_id=body.provider_account_id,
            connector_id=spec.connector_id,
            contract_version=body.contract_version,
            payload_canonical=canonical(payload),
        )
    if not admitted.created:
        response.status_code = 200
    return Admitted(operation_id=admitted.operation_id, created=admitted.created)


@router.get("/actions", response_model=OperationPage)
def list_actions(
    request: Request,
    state: str | None = Query(None, pattern=r"^[A-Z_]{1,32}$"),
    connector: str | None = Query(None, pattern=IDENTIFIER),
    unresolved: bool = False,
    before: datetime | None = None,
    before_id: UUID | None = None,
    limit: int = Query(50, ge=1, le=100),
):
    if (before is None) != (before_id is None):
        raise ApiError(422, "invalid_request", "The request is not valid.")
    with transaction(request) as connection:
        rows = product.operations(
            connection, schema(request), secret=secret(request),
            state=state, connector=connector, unresolved_only=unresolved,
            before=before, before_id=before_id, limit=limit,
        )
    last = rows[-1] if len(rows) == limit else None
    return OperationPage(
        items=[OperationSummary(**vars_of(row)) for row in rows],
        next_before=last.created_at if last else None,
        next_before_id=last.operation_id if last else None,
    )


@router.get("/actions/{operation_id}")
def get_action(operation_id: UUID, request: Request):
    """Full investigation view. Provider text is untrusted: render as text."""

    with transaction(request) as connection:
        return product.operation(
            connection, schema(request), secret=secret(request),
            operation_id=operation_id,
        )


@router.post("/actions/{operation_id}/cancel", response_model=CancelResult)
def cancel(operation_id: UUID, request: Request):
    with transaction(request) as connection:
        result = product.cancel(
            connection, schema(request), secret=secret(request),
            csrf=csrf(request), operation_id=operation_id,
        )
    return CancelResult(outcome=result.outcome, state=result.state,
                        seq=result.seq)


@router.post(
    "/workflows", response_model=WorkflowAdmitted, status_code=201,
    dependencies=[Depends(no_duplicate_keys)],
)
def create_workflow(body: CreateWorkflow, request: Request,
                    response: Response):
    charge = {"customer_id": body.customer_id, "amount_minor": body.fee_minor}
    order = {"case_id": body.case_id, "sku": body.sku}
    with transaction(request) as connection:
        admitted = product.create_workflow(
            connection, schema(request),
            secret=secret(request), csrf=csrf(request),
            request_key=body.request_key,
            customer_id=body.customer_id, case_id=body.case_id,
            sku=body.sku, fee_minor=body.fee_minor,
            payments_account_id=body.payments_account_id,
            crm_account_id=body.crm_account_id,
            charge_contract=body.charge_contract,
            order_contract=body.order_contract,
            compensation_contract=body.compensation_contract,
            charge_payload=canonical_payload_bytes(charge).decode("utf-8"),
            order_payload=canonical_payload_bytes(order).decode("utf-8"),
        )
    if not admitted.created:
        response.status_code = 200
    return WorkflowAdmitted(
        workflow_id=admitted.workflow_id,
        charge_operation_id=admitted.charge_operation_id,
        created=admitted.created,
    )


@router.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: UUID, request: Request):
    with transaction(request) as connection:
        return product.workflow(
            connection, schema(request), secret=secret(request),
            workflow_id=workflow_id,
        )


def vars_of(row):
    return {name: getattr(row, name) for name in row.__slots__}
