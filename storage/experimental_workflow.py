"""Fixed two-step workflow: creation and advancement (experimental).

Creation records the whole plan (order step and compensation policy)
and admits the charge through the normal admission path, all in one
transaction, keyed by (tenant, request key) so a repeated or ack-lost
create returns the same workflow (F11).

Advancement never decides anything from caller data: the advancer asks
the database which workflows are ready and calls the protected step
function, which checks preconditions and builds the next operation from
the recorded plan (or, for compensation, from the forward record).
"""

from dataclasses import dataclass
from contextlib import closing
from uuid import UUID, uuid4

import psycopg
import sqlalchemy as sa

from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.contracts import canonical_payload_bytes
from core.workflow import CHARGE, ORDER, STEP_ACTIONS, WORKFLOW_TYPE
from storage import durable_lifecycle as lifecycle
from storage.schema import operations
from storage.submission import IntentSubmission, record_intent


_meta = sa.MetaData()

workflows = sa.Table(
    "safeact_workflows", _meta,
    sa.Column("workflow_id", PgUUID(as_uuid=True), primary_key=True),
    sa.Column("tenant_id", sa.String(128)),
    sa.Column("request_key", sa.String(255)),
    sa.Column("workflow_type", sa.String(64)),
    sa.Column("created_by", sa.String(128)),
    sa.Column("plan", JSONB),
)

workflow_steps = sa.Table(
    "safeact_workflow_steps", _meta,
    sa.Column("workflow_id", PgUUID(as_uuid=True)),
    sa.Column("step", sa.String(16)),
    sa.Column("operation_id", PgUUID(as_uuid=True)),
    sa.Column("compensates_operation_id", PgUUID(as_uuid=True)),
)


class WorkflowConflict(ValueError):
    """The request key was already used for a different plan."""


@dataclass(frozen=True, slots=True)
class WorkflowRequest:
    tenant_id: str
    request_key: str
    actor_id: str
    customer_id: str
    case_id: str
    sku: str
    fee_minor: int
    payments_account_id: str
    crm_account_id: str
    charge_contract: str = "v1"
    order_contract: str = "v1"
    compensation_contract: str = "v1"

    def __post_init__(self):
        if type(self.fee_minor) is not int or self.fee_minor <= 0:
            raise ValueError("fee must be a positive integer")


@dataclass(frozen=True, slots=True)
class WorkflowReceipt:
    workflow_id: UUID
    charge_operation_id: UUID
    created: bool


def _plan(request):
    order_connector, order_action = STEP_ACTIONS[ORDER]
    order_payload = {"case_id": request.case_id, "sku": request.sku}
    return {
        "order": {
            "action_type": order_action,
            "resource_id": request.case_id,
            "provider_account_id": request.crm_account_id,
            "connector_id": order_connector,
            "contract_version": request.order_contract,
            "payload_canonical":
                canonical_payload_bytes(order_payload).decode("utf-8"),
        },
        # Authorized with the workflow: refund exactly the charge, and
        # only when the order is confirmed failed.
        "compensation": {
            "policy": "refund_charge_when_order_failed_confirmed",
            "action_type": "refund_payment",
            "connector_id": "mock-payments",
            "provider_account_id": request.payments_account_id,
            "contract_version": request.compensation_contract,
        },
    }


def _charge(request, workflow_id):
    connector, action = STEP_ACTIONS[CHARGE]
    return IntentSubmission(
        tenant_id=request.tenant_id,
        request_key=f"wf:{workflow_id}:charge",
        action_type=action,
        resource_id=request.customer_id,
        provider_account_id=request.payments_account_id,
        connector_id=connector,
        contract_version=request.charge_contract,
        actor_id=request.actor_id,
        payload={
            "customer_id": request.customer_id,
            "amount_minor": request.fee_minor,
        },
    )


def create_workflow(engine, request):
    """Record the plan and admit the charge, atomically and idempotently."""

    plan = _plan(request)
    with engine.begin() as connection:
        workflow_id = connection.execute(
            pg_insert(workflows).values(
                workflow_id=uuid4(),
                tenant_id=request.tenant_id,
                request_key=request.request_key,
                workflow_type=WORKFLOW_TYPE,
                created_by=request.actor_id,
                plan=plan,
            ).on_conflict_do_nothing(
                constraint="uq_safeact_workflows_tenant_request"
            ).returning(workflows.c.workflow_id)
        ).scalar_one_or_none()

        if workflow_id is None:
            return _existing(connection, request, plan)

        receipt = record_intent(connection, _charge(request, workflow_id))
        connection.execute(sa.insert(workflow_steps).values(
            workflow_id=workflow_id, step=CHARGE,
            operation_id=receipt.operation_id,
        ))
        return WorkflowReceipt(workflow_id, receipt.operation_id, True)


def _existing(connection, request, plan):
    row = connection.execute(
        sa.select(workflows).where(
            workflows.c.tenant_id == request.tenant_id,
            workflows.c.request_key == request.request_key,
        )
    ).mappings().one()
    if row["plan"] != plan or row["created_by"] != request.actor_id:
        raise WorkflowConflict(
            "Workflow request key reused for a different plan"
        )
    charge = connection.execute(
        sa.select(workflow_steps.c.operation_id).where(
            workflow_steps.c.workflow_id == row["workflow_id"],
            workflow_steps.c.step == CHARGE,
        )
    ).scalar_one()
    # The stored plan holds the order and inverse policy, not the initial
    # charge. Bind replays to that immutable operation too, including after
    # its lifecycle has advanced. This mirrors the protected API's 0021 check.
    original = connection.execute(
        sa.select(operations).where(operations.c.operation_id == charge)
    ).mappings().one()
    expected = _charge(request, row["workflow_id"])
    fields = ("tenant_id", "request_key", "connector_id", "action_type",
              "resource_id", "provider_account_id", "contract_version")
    if (any(original[field] != getattr(expected, field) for field in fields)
            or original["payload_canonical"] != canonical_payload_bytes(expected.payload).decode("utf-8")):
        raise WorkflowConflict("Workflow request key reused for a different charge")
    return WorkflowReceipt(row["workflow_id"], charge, False)


UNCERTAIN_ERRORS = (
    psycopg.OperationalError, psycopg.InterfaceError,
    ConnectionError, OSError,
)


class WorkflowAdvancer:
    """Admits the next step of ready workflows; decides nothing itself."""

    def __init__(self, *, connect, schema, fault_hook=None, connection_scope=None):
        self._connect = connect
        self._connection_scope = connection_scope or (lambda: closing(connect()))
        self._schema = schema
        self._fault = fault_hook or (lambda point, connection: None)

    def advance_once(self, *, limit=100):
        with self._connection_scope() as connection:
            ready = lifecycle.workflows_to_advance(
                connection, self._schema, limit=limit
            )
            connection.commit()

            admitted = []
            for workflow_id, step in ready:
                admitted.append(self.advance(connection, workflow_id, step))
            return admitted

    def advance(self, connection, workflow_id, step):
        """Admit one step; on an uncertain commit, ask again.

        The step function is idempotent, so asking again after a lost
        acknowledgement returns the operation that was admitted, or
        admits it now. It never admits a second one.
        """

        try:
            operation_id = lifecycle.admit_workflow_step(
                connection, self._schema, workflow_id, step
            )
            self._fault("before_advance_commit", connection)
            connection.commit()
            self._fault("after_advance_commit", connection)
            return workflow_id, step, operation_id
        except lifecycle.LifecycleRejected:
            connection.rollback()
            return workflow_id, step, None
        except UNCERTAIN_ERRORS:
            pass

        observer = self._connect()
        try:
            operation_id = lifecycle.admit_workflow_step(
                observer, self._schema, workflow_id, step
            )
            observer.commit()
            return workflow_id, step, operation_id
        finally:
            observer.close()
