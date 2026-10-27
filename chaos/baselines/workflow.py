"""B3's fixed workflow, using its own ordinary transactions and child jobs."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from chaos.baselines.recovery import Fact
from chaos.baselines.store import Conflict, Request, admit_in
from chaos.profiles import profile
from core.workflow import workflow_status


class CorruptWorkflow(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkflowRequest:
    tenant_id: str
    requester_id: str
    request_key: str
    customer_id: str
    case_id: str
    sku: str
    fee_minor: int
    payments_account_id: str
    crm_account_id: str
    charge_contract: str = "q-v2"
    order_contract: str = "q-v2"
    compensation_contract: str = "q-v2"

    def __post_init__(self):
        if type(self.fee_minor) is not int or not 0 < self.fee_minor < 2**31:
            raise ValueError("A bounded positive integer fee is required")
        # Validate all identities/payload bounds with the same intent contract.
        for step in ("charge","order","compensation"):
            replace(child(self,UUID(int=0),step,charge_reference="validation-charge"),request_key=self.request_key)
        if type(self.sku) is not str or not self.sku.strip():
            raise ValueError("An explicit replacement SKU is required")


@dataclass(frozen=True)
class Receipt:
    workflow_id: UUID
    charge_operation_id: UUID
    created: bool


def child(request, workflow_id, step, *, charge_reference=None):
    if step=="charge":
        connector,action,resource,account,version,payload = (
            "mock-payments","charge_shipping_fee",request.customer_id,request.payments_account_id,
            request.charge_contract,dict(customer_id=request.customer_id,amount_minor=request.fee_minor))
    elif step=="order":
        connector,action,resource,account,version,payload = (
            "mock-crm","create_replacement_order",request.case_id,request.crm_account_id,
            request.order_contract,dict(case_id=request.case_id,sku=request.sku))
    elif step=="compensation" and charge_reference:
        connector,action,resource,account,version,payload = (
            "mock-payments","refund_payment",charge_reference,request.payments_account_id,
            request.compensation_contract,dict(charge_id=charge_reference,amount_minor=request.fee_minor))
    else:
        raise ValueError("A fixed step and actual charge reference are required")
    profile(connector,version)
    return Request.from_payload(tenant_id=request.tenant_id,requester_id=request.requester_id,
        request_key=f"wf:{workflow_id}:{step}",connector_id=connector,action_type=action,
        resource_id=resource,provider_account_id=account,contract_version=version,payload=payload)


class Workflow:
    def __init__(self, runtime):
        self.runtime,self.t,self.engine = runtime,runtime.t,runtime.engine

    @contextmanager
    def transaction(self, stage, workflow_id):
        details = dict(workflow_id=str(workflow_id))
        with self.engine.begin() as c:
            c.info["workflow_mutated"] = False
            yield c
            mutated = c.info.pop("workflow_mutated",False)
            if mutated:
                details["commit_xid"] = c.scalar(sa.text("SELECT pg_current_xact_id()::text"))
                self.runtime.hook(f"before_{stage}_commit",details)
        if mutated:
            self.runtime.hook(f"after_{stage}_commit",details)

    def audit(self,c,workflow_id,kind,**details):
        c.execute(self.t.workflow_audit.insert().values(workflow_id=workflow_id,kind=kind,details=details))
        c.info["workflow_mutated"] = True

    def create(self,request):
        if not isinstance(request,WorkflowRequest):
            raise TypeError("A typed immutable workflow request is required")
        candidate = uuid4()
        with self.transaction("workflow_admission",candidate) as c:
            if c.get_isolation_level()!="READ COMMITTED":
                raise ValueError("Workflow admission requires READ COMMITTED isolation")
            workflow_id = c.execute(insert(self.t.workflows).values(workflow_id=candidate,
                tenant_id=request.tenant_id,requester_id=request.requester_id,request_key=request.request_key,
                request=asdict(request)).on_conflict_do_nothing().returning(self.t.workflows.c.workflow_id)).scalar_one_or_none()
            if workflow_id is None:
                row = c.execute(sa.select(self.t.workflows).where(self.t.workflows.c.tenant_id==request.tenant_id,
                    self.t.workflows.c.request_key==request.request_key)).mappings().one()
                if row["request"]!=asdict(request) or row["requester_id"]!=request.requester_id:
                    raise Conflict("Workflow identity conflicts with its original complete request")
                steps = self.bindings(c,row)
                return Receipt(row["workflow_id"],steps["charge"]["operation_id"],False)
            charge = admit_in(c,self.t,child(request,workflow_id,"charge"))
            c.execute(self.t.steps.insert().values(workflow_id=workflow_id,step="charge",operation_id=charge.operation_id))
            self.audit(c,workflow_id,"workflow_created",charge_operation_id=str(charge.operation_id))
        return Receipt(workflow_id,charge.operation_id,True)

    def applied_reference(self,c,row):
        facts,_ = self.runtime.history(c,row)
        refs = {f.reference for f in facts if f.fact is Fact.APPLIED}
        if len(refs)!=1:
            raise CorruptWorkflow("A successful charge needs one actual applied reference")
        return refs.pop()

    def bindings(self,c,workflow):
        # The insert-only workflow/step tables need no UPDATE grant. Its
        # original mutable charge job is the coordination row. Acquire it
        # BEFORE reading bindings so a waiting advancer sees the winner's step.
        original = c.execute(sa.select(self.t.jobs.c.operation_id).join(self.t.steps,
            self.t.steps.c.operation_id==self.t.jobs.c.operation_id).where(
            self.t.steps.c.workflow_id==workflow["workflow_id"],self.t.steps.c.step=="charge")
            .with_for_update(of=self.t.jobs)).scalar_one_or_none()
        if original is None:
            raise CorruptWorkflow("Workflow has no original charge binding")
        request = WorkflowRequest(**workflow["request"])
        if (workflow["tenant_id"],workflow["requester_id"],workflow["request_key"])!=(
                request.tenant_id,request.requester_id,request.request_key):
            raise CorruptWorkflow("Workflow identity differs from its immutable request")
        bindings = c.execute(sa.select(self.t.steps).where(
            self.t.steps.c.workflow_id==workflow["workflow_id"])).mappings().all()
        links = {s["step"]:s for s in bindings}
        if "charge" not in links:
            raise CorruptWorkflow("Workflow has no original charge binding")
        result = {}
        for step in ("charge","order","compensation"):
            if step not in links:continue
            row = self.runtime.job(c,links[step]["operation_id"])
            reference = self.applied_reference(c,result["charge"]) if step=="compensation" else None
            expected = child(request,workflow["workflow_id"],step,charge_reference=reference).immutable()
            if row is None or any(row[k]!=v for k,v in expected.items()):
                raise CorruptWorkflow("Workflow child differs from its immutable authorized binding")
            facts,_ = self.runtime.history(c,row)
            if row["status"]=="SUCCEEDED" and not any(f.fact is Fact.APPLIED for f in facts):
                raise CorruptWorkflow("Success lacks actual applied evidence")
            if row["status"]=="FAILED_CONFIRMED" and (not facts or not all(f.fact is Fact.NOT_APPLIED for f in facts)):
                raise CorruptWorkflow("Confirmed failure lacks proof for every attempt")
            if step=="compensation" and links[step]["compensates_operation_id"]!=result["charge"]["operation_id"]:
                raise CorruptWorkflow("Inverse does not link to the original charge")
            result[step] = dict(row)
        try:
            workflow_status(*(result[s]["status"] if s in result else None for s in ("charge","order","compensation")))
        except ValueError as error:
            raise CorruptWorkflow("Workflow child preconditions are inconsistent") from error
        return result

    def advance(self,workflow_id):
        with self.transaction("workflow_advance",workflow_id) as c:
            row = c.execute(sa.select(self.t.workflows).where(self.t.workflows.c.workflow_id==workflow_id)
                ).mappings().one_or_none()
            if row is None:return None
            steps = self.bindings(c,row)
            if steps["charge"]["status"]!="SUCCEEDED":return None
            if "order" not in steps:step="order"
            elif steps["order"]["status"]=="FAILED_CONFIRMED" and "compensation" not in steps:step="compensation"
            else:return None
            request = WorkflowRequest(**row["request"])
            reference = self.applied_reference(c,steps["charge"]) if step=="compensation" else None
            receipt = admit_in(c,self.t,child(request,workflow_id,step,charge_reference=reference))
            if not receipt.created:
                raise CorruptWorkflow("Unbound workflow identity already exists")
            inverse = steps["charge"]["operation_id"] if step=="compensation" else None
            c.execute(self.t.steps.insert().values(workflow_id=workflow_id,step=step,
                operation_id=receipt.operation_id,compensates_operation_id=inverse))
            self.audit(c,workflow_id,"step_admitted",step=step,operation_id=str(receipt.operation_id),
                       compensates_operation_id=str(inverse) if inverse else None)
            return receipt.operation_id

    def advance_once(self,*,limit=100):
        s,j = self.t.steps,self.t.jobs
        ch,order,inverse = s.alias("charge"),s.alias("order_step"),s.alias("inverse")
        cj,oj = j.alias("charge_job"),j.alias("order_job")
        with self.engine.connect() as c:
            ids = c.scalars(sa.select(self.t.workflows.c.workflow_id).join(ch,sa.and_(
                ch.c.workflow_id==self.t.workflows.c.workflow_id,ch.c.step=="charge"))
                .join(cj,cj.c.operation_id==ch.c.operation_id)
                .outerjoin(order,sa.and_(order.c.workflow_id==ch.c.workflow_id,order.c.step=="order"))
                .outerjoin(oj,oj.c.operation_id==order.c.operation_id)
                .outerjoin(inverse,sa.and_(inverse.c.workflow_id==ch.c.workflow_id,inverse.c.step=="compensation"))
                .where(cj.c.status=="SUCCEEDED",sa.or_(order.c.operation_id.is_(None),
                    sa.and_(oj.c.status=="FAILED_CONFIRMED",inverse.c.operation_id.is_(None))))
                .order_by(self.t.workflows.c.created_at).limit(limit)).all()
        return {w:self.advance(w) for w in ids}

    def inspect(self,workflow_id):
        with self.engine.begin() as c:
            row = c.execute(sa.select(self.t.workflows).where(self.t.workflows.c.workflow_id==workflow_id)
                ).mappings().one()
            steps = self.bindings(c,row)
            status = workflow_status(*(steps[s]["status"] if s in steps else None
                                       for s in ("charge","order","compensation"))).value
            return dict(workflow_id=workflow_id,request=row["request"],steps=steps,status=status)
