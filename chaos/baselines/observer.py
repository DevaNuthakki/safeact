"""Independent, read-only B3 database observations for the shared harness.

No runtime methods, coordination locks, provider ledger, humans or sessions are
read here. A sample is one committed repeatable-read snapshot. Its monotonic
interval brackets queries; database clock comparisons establish expiry.
"""

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa

from chaos.arms.base import OperationView
from chaos.baselines.keys import payment_key
from chaos.database_trace import _interval, audit_rows, breaker_rows, lease_rows, recovery_rows
from chaos.profiles import profile
from connectors.http_trace import correlation_hash
from core.workflow import workflow_status
from chaos.workflow_trace import outcome_class


class MissingBinding(RuntimeError):
    pass


def epoch(value):
    return value.timestamp() if value is not None else None


@dataclass(frozen=True)
class Observation:
    operations: dict
    database: list


class Observer:
    def __init__(self, engine, tables, *, mode='b3'):
        self.engine, self.t = engine, tables
        if mode not in ('b1','b2','b3'):raise ValueError('Unknown baseline observation mode')
        self.mode=mode

    @contextmanager
    def snapshot(self):
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as c:
            with c.begin():
                c.execute(sa.text("SET TRANSACTION READ ONLY"))
                yield c

    def sample(self, *, operation_ids=(), workflow_ids=()):
        requested = {UUID(str(i)) for i in operation_ids}
        workflow_ids = {UUID(str(i)) for i in workflow_ids}
        if not requested and not workflow_ids:
            return Observation({}, [])
        started = time.monotonic_ns()
        with self.snapshot() as c:
            roots = c.execute(sa.select(self.t.workflows).where(
                self.t.workflows.c.workflow_id.in_(workflow_ids))).mappings().all() if workflow_ids else []
            if {r["workflow_id"] for r in roots} != workflow_ids:
                raise MissingBinding("Admitted B3 workflow is missing")
            links = c.execute(sa.select(self.t.steps).where(
                self.t.steps.c.workflow_id.in_(workflow_ids)).order_by(
                    self.t.steps.c.workflow_id, self.t.steps.c.step)).mappings().all() if workflow_ids else []
            ids = requested | {r["operation_id"] for r in links}
            jobs = c.execute(sa.select(self.t.jobs).where(
                self.t.jobs.c.operation_id.in_(ids)).order_by(self.t.jobs.c.operation_id)).mappings().all()
            if {r["operation_id"] for r in jobs} != ids:
                raise MissingBinding("Admitted B3 operation or workflow child is missing")
            histories = {}
            samples, events, views = {}, [], {}
            for row in jobs:
                op = row["operation_id"]
                audits = c.execute(sa.select(self.t.audit).where(self.t.audit.c.operation_id==op)
                    .order_by(self.t.audit.c.audit_id)).mappings().all()
                attempts = c.execute(sa.select(self.t.attempts).where(self.t.attempts.c.operation_id==op)
                    .order_by(self.t.attempts.c.ordinal)).mappings().all()
                count = c.scalar(sa.select(sa.func.count()).select_from(self.t.observations).where(
                    self.t.observations.c.operation_id==op))
                contract = profile(row["connector_id"], row["contract_version"])
                # This is the wire identity: only native-key connectors send b3_<uuid>.
                ordinal=attempts[-1]['ordinal'] if attempts else 1
                key = payment_key(op,ordinal,self.mode) if contract.contract.native_idempotency else str(op)
                transitions = [a for a in audits if a["details"].get("to_state")]
                changed = epoch(transitions[-1]["created_at"] if transitions else row["created_at"])
                sample = dict(event="db_operation_sample", operation_id=str(op),
                    tenant_id=row["tenant_id"], requester_id=row["requester_id"], request_key=row["request_key"],
                    connector_id=row["connector_id"], contract_version=row["contract_version"],
                    action_type=row["action_type"], resource_id=row["resource_id"],
                    provider_account_id=row["provider_account_id"], payload=json.loads(row["payload_canonical"]),
                    correlation_sha256=correlation_hash(key), state=row["status"], transition_seq=row["version"],
                    state_changed_at=changed, resolved_at=epoch(row["resolved_at"]),
                    escalated_at=epoch(row["escalated_at"]), evidence_rows=count)
                samples[op] = sample
                if self.mode!='b3':sample.update(native_key_mode=self.mode,attempt_ordinals=[a['ordinal'] for a in attempts],
                    correlation_sha256s=sorted({correlation_hash(payment_key(op,a['ordinal'],self.mode) if contract.contract.native_idempotency else str(op))
                        for a in attempts} or {correlation_hash(key)}))
                if self.mode!='b3':sample['applied_reference']=c.scalar(sa.select(self.t.queue_results.c.reference).where(self.t.queue_results.c.operation_id==op))
                histories[op] = [(epoch(a["created_at"]), a["details"].get("to_state", a["kind"])) for a in audits]
                timing = _interval(started, time.monotonic_ns())
                events.append(dict(sample, **timing))
                events.extend(audit_rows([(a["audit_id"], op, a["kind"], a["commit_xid"], epoch(a["created_at"]))
                    for a in audits], started, time.monotonic_ns()))
                if row["status"]=="DISPATCHING":
                    current = [a for a in attempts if a["claim_token"]==row["claim_token"]]
                    if (len(current)!=1 or row["lease_until"] is None
                            or current[0]['fenced_version']!=row['version'] or current[0]['lease_until']!=row['lease_until']):
                        raise MissingBinding("Dispatch has no matching committed attempt and lease")
                for attempt in attempts:
                    active=c.scalar(sa.select(self.t.attempts.c.lease_until>sa.func.clock_timestamp()).where(
                        self.t.attempts.c.attempt_id==attempt['attempt_id']))
                    events.extend(lease_rows([(op,attempt['fenced_version'],attempt['claim_token'],attempt['attempt_id'],
                        epoch(attempt['lease_until']),active,row['connector_id'],
                        payment_key(op,attempt['ordinal'],self.mode) if contract.contract.native_idempotency else str(op))],started,time.monotonic_ns()))
                deadline = attempts[-1 if self.mode=='b3' else 0]["fenced_at"]+contract.investigation_window if attempts else None
                passed = c.scalar(sa.select(sa.func.clock_timestamp() >= deadline)) if deadline else None
                recovery_keys = {key} if self.mode == 'b3' else {
                    payment_key(op,a['ordinal'],self.mode) if contract.contract.native_idempotency else str(op)
                    for a in attempts} or {key}
                events.extend(recovery_rows([(op,row["version"],row["status"],row["connector_id"],identity,
                    epoch(deadline),passed,epoch(row["escalated_at"])) for identity in sorted(recovery_keys)],
                    started, time.monotonic_ns()))
                health = c.execute(sa.select(self.t.accounts,
                    (self.t.accounts.c.open_until > sa.func.clock_timestamp()).label('active')).where(
                    self.t.accounts.c.connector_id == row['connector_id'],
                    self.t.accounts.c.provider_account_id == row['provider_account_id'])).mappings().one_or_none()
                if health is not None and health['open_until'] is not None:
                    events.extend(breaker_rows([(op, row['version'], row['status'], row['connector_id'], identity,
                        row['provider_account_id'], health['failures'], contract.limits.breaker_threshold,
                        epoch(health['open_until']), health['active']) for identity in sorted(recovery_keys)],
                        started, time.monotonic_ns()))
                outcome = {"SUCCEEDED":"confirmed_success", "FAILED_CONFIRMED":"confirmed_failure",
                           "ESCALATED_UNRESOLVED":"escalated_unresolved"}.get(row["status"], "pending")
                views[str(op)] = OperationView(str(op), outcome, row["status"], histories[op],
                    epoch(row["resolved_at"]), epoch(row["escalated_at"]), count)
            for root in roots:
                children = [dict(samples[s["operation_id"]], step=s["step"],
                    compensates_operation_id=str(s["compensates_operation_id"]) if s["compensates_operation_id"] else None)
                    for s in links if s["workflow_id"]==root["workflow_id"]]
                by_role = {s["step"]:s for s in children}
                if "charge" not in by_role:
                    raise MissingBinding("Workflow has no original charge binding")
                try:
                    status = workflow_status(*(by_role[s]["state"] if s in by_role else None
                        for s in ("charge","order","compensation"))).value
                except ValueError:
                    status = "INVALID"
                events.append(dict(_interval(started,time.monotonic_ns()), event="db_workflow_sample",
                    binding_format="b3-request-v1" if self.mode=='b3' else 'queue-request-v1', workflow_id=str(root["workflow_id"]),
                    tenant_id=root["tenant_id"],requester_id=root["requester_id"],request_key=root["request_key"],
                    request=root["request"], steps=children, status=status, status_source="derived_child_states"))
                outcome = outcome_class(status, children)
                if self.mode != 'b3' and status in ('UNRESOLVED', 'COMPENSATION_UNRESOLVED'):
                    outcome = 'pending'  # Queue-only arms have no manual escalation.
                resolved = [s["resolved_at"] for s in children if s["resolved_at"] is not None]
                escalated = [s["escalated_at"] for s in children if s["escalated_at"] is not None]
                views[str(root["workflow_id"])] = OperationView(str(root["workflow_id"]),outcome,status,
                    sorted((at,s["step"]+":"+label) for s in children
                           for at,label in histories[UUID(s["operation_id"])]),
                    max(resolved) if resolved and outcome in ("confirmed_success","confirmed_failure","verified_compensation") else None,
                    min(escalated) if escalated else None,sum(s["evidence_rows"] for s in children))
        # Do not expose children as separately admitted operations.
        return Observation({str(i):views[str(i)] for i in requested | workflow_ids}, events)
