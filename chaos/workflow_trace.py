"""Committed workflow/step bindings read by the qualification observer."""

import json
import time

from psycopg import sql

from chaos.database_trace import _interval
from connectors.http_trace import correlation_hash


def outcome_class(status, steps):
    completed = {'COMPLETED': 'confirmed_success', 'FAILED_NO_EFFECT': 'confirmed_failure',
                 'COMPENSATED_VERIFIED': 'verified_compensation'}
    if status in completed:
        return completed[status]
    if status in ('UNRESOLVED', 'COMPENSATION_UNRESOLVED') and any(
            step.get('state') == 'ESCALATED_UNRESOLVED' or step.get('escalated_at') is not None for step in steps):
        return 'escalated_unresolved'
    return 'pending'


def sample_workflows(connection, schema, workflow_ids):
    if not workflow_ids:
        return []
    started = time.monotonic_ns()
    rows = connection.execute(sql.SQL("""
        SELECT w.workflow_id, w.tenant_id, w.request_key, w.plan, v.status,
               st.step, o.operation_id, st.compensates_operation_id,
               o.connector_id, o.action_type, o.resource_id,
               o.provider_account_id, o.contract_version, o.payload_canonical,
               coalesce(o.provider_idempotency_key, o.operation_id::text),
               p.state, p.seq, extract(epoch FROM p.state_changed_at),
               (SELECT count(*) FROM {s}.safeact_attempt_outcomes e
                WHERE e.operation_id = o.operation_id),
               (SELECT min(extract(epoch FROM t.created_at))
                FROM {s}.safeact_operation_transitions t
                WHERE t.operation_id = o.operation_id
                  AND t.to_state IN ('SUCCEEDED', 'FAILED_CONFIRMED')),
               (SELECT min(extract(epoch FROM t.created_at))
                FROM {s}.safeact_operation_transitions t
                WHERE t.operation_id = o.operation_id
                  AND t.to_state = 'ESCALATED_UNRESOLVED')
        FROM {s}.safeact_workflows w
        JOIN {s}.safeact_workflow_status v USING (workflow_id)
        JOIN {s}.safeact_workflow_steps st USING (workflow_id)
        JOIN {s}.safeact_operations o USING (operation_id)
        JOIN {s}.safeact_state_projection p USING (operation_id)
        WHERE w.workflow_id = ANY(%s::uuid[])
        ORDER BY w.workflow_id, st.step
    """).format(s=sql.Identifier(schema)), (list(workflow_ids),)).fetchall()
    timing = _interval(started, time.monotonic_ns())
    samples = {}
    for (workflow, tenant, request, plan, status, step, operation, compensates,
         connector, action, resource, account, contract, payload, key, state,
         seq, changed, evidence, resolved, escalated) in rows:
        entry = samples.setdefault(str(workflow), dict(timing,
            event="db_workflow_sample", workflow_id=str(workflow),
            tenant_id=tenant, request_key=request, plan=plan, status=status, steps=[]))
        entry["steps"].append(dict(step=step, operation_id=str(operation),
            compensates_operation_id=str(compensates) if compensates else None,
            connector_id=connector, action_type=action, resource_id=resource,
            provider_account_id=account, contract_version=contract,
            payload=json.loads(payload), correlation_sha256=correlation_hash(key),
            state=state, transition_seq=seq, evidence_rows=evidence,
            state_changed_at=float(changed) if changed is not None else None,
            resolved_at=float(resolved) if resolved is not None else None,
            escalated_at=float(escalated) if escalated is not None else None))
    return list(samples.values())


def operation_ids(samples):
    return sorted({step["operation_id"] for sample in samples for step in sample["steps"]})
