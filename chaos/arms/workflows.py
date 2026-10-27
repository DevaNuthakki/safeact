"""The SafeAct arm's fixed-workflow admission and independent observations."""

import psycopg

from chaos.arms.base import Admission, OperationView
from chaos.workflow_trace import operation_ids, outcome_class, sample_workflows
from storage.database import local_admin_settings
from storage.experimental_workflow import WorkflowConflict, WorkflowRequest, create_workflow


class SafeActWorkflows:
    def workflow_submit(self, scenario, index, operation, *, changed=False):
        if len(scenario.operations) != 1 or index != 0:
            raise ValueError("Fixed workflow qualification requires one requested workflow")
        spec = scenario.workflow
        try:
            receipt = create_workflow(self.engine, WorkflowRequest(
                tenant_id="tenant-north", request_key=f"{scenario.scenario_id}-{index}",
                actor_id="qualification", customer_id=spec.customer_id,
                case_id=spec.case_id, sku=spec.sku + ("-changed" if changed else ""),
                fee_minor=operation.amount_minor,
                payments_account_id=self.account(scenario),
                crm_account_id=self.account(scenario), charge_contract=spec.charge_contract,
                order_contract=spec.order_contract, compensation_contract=spec.compensation_contract))
        except WorkflowConflict:
            return Admission(None, "conflict")
        with self._broker_ids_lock:
            self._workflow_ids.add(str(receipt.workflow_id))
            self._broker_ids.add(str(receipt.charge_operation_id))
        return Admission(str(receipt.workflow_id))

    def _workflow_samples(self, connection, ids):
        samples = sample_workflows(connection, self.env.schema, ids)
        self._database_samples.extend(samples)
        with self._broker_ids_lock:
            self._broker_ids.update(operation_ids(samples))
        return samples

    def sample_workflows_before_refund(self):
        try:
            with self._broker_ids_lock:
                ids = sorted(self._workflow_ids)
            if not ids:
                raise RuntimeError("Refund arrived without an admitted qualification workflow")
            with psycopg.connect(**dict(local_admin_settings(), connect_timeout=2),
                                 autocommit=True) as observer:
                observer.execute("SET statement_timeout = '2s'")
                self._workflow_samples(observer, ids)
        except Exception as error:
            # A failed observer must fail the harness, even if the HTTP API
            # translates the exception into a provider error and work recovers.
            self._workflow_observation_errors.append(type(error).__name__)
            raise

    def execution_ids(self, local_ids):
        if self._workflow_spec is None:
            return local_ids
        return operation_ids(self._workflow_samples(self.admin, local_ids))

    def workflow_observe(self, local_ids):
        samples = {s["workflow_id"]: s for s in self._workflow_samples(self.admin, local_ids)}
        result = {}
        for local_id in local_ids:
            sample = samples.get(local_id)
            if sample is None:
                result[local_id] = OperationView(local_id, "pending", "MISSING")
                continue
            steps = sample["steps"]
            outcome = outcome_class(sample['status'], steps)
            resolved = [s["resolved_at"] for s in steps if s["resolved_at"] is not None]
            escalated = [s["escalated_at"] for s in steps if s["escalated_at"] is not None]
            result[local_id] = OperationView(local_id=local_id, outcome_class=outcome,
                state=sample["status"],
                events=[(s["state_changed_at"], f"{s['step']}:{s['state']}")
                        for s in steps if s["state_changed_at"] is not None],
                resolved_at=max(resolved) if resolved and outcome in (
                    "confirmed_success", "confirmed_failure", "verified_compensation") else None,
                escalated_at=min(escalated) if escalated else None,
                evidence_rows=sum(s["evidence_rows"] for s in steps))
        return result
