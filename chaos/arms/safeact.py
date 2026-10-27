"""The SafeAct arm: the real system, as separate processes behind Toxiproxy.

Admission is SafeAct's own (storage.submission, the transactional gate);
the worker (prefork Celery), relay, reconciler and advancer run as their
own processes with their own database roles, exactly as deployed, and
reach the providers only through the slot's proxies. Deterministic
failpoints come from the slot's failpoint directory. A worker or
reconciler killed by a failpoint is restarted before the next scenario,
as a supervisor would.
"""

import time
import threading
import uuid

from pathlib import Path

import psycopg
import sqlalchemy as sa

from psycopg import sql

from chaos.arms.base import Admission, Arm, OperationView
from chaos.arms.workflows import SafeActWorkflows
from chaos.arms.boundaries import SafeActBoundaries
from chaos.broker_loss import QueuedLoss, CrashedBrokerLoss
from chaos.broker_redelivery import DispatchRedelivery, restore_unacked
from chaos.database_trace import audit_rows, lease_rows, sample_breakers, sample_recovery
from chaos.recovery_faults import release_after_escalation
from chaos.profiles import LEASE_SECONDS, QUAL_PROFILES
from chaos.quiescence import kill_parked_worker, stop_processes
from chaos.scenarios import CONNECTORS
from core.provider_keys import derive_provider_key
from deploy.demo.bootstrap import (
    DEMO_ACCOUNTS, Environment, credential_lines, rebuild,
    teardown_statements, write_credentials,
)
from deploy.stack.processes import ProcessStack
from deploy.stack.http_trace import read_trace
from deploy.stack.execution_trace import ERROR as EXEC_TRACE_ERROR, read_trace as read_execution
from deploy.stack.broker_trace import ERROR as BROKER_TRACE_ERROR, read_trace as read_broker
from connectors.http_trace import TRACE_ERROR
from storage.database import local_admin_settings, local_database_url
from storage.connection_pool import reset_engine_sessions
from storage.state_projection import projection_drift
from storage.submission import (
    IntentSubmission, SubmissionConflict, submit_intent,
)


TENANT = "tenant-north"
CLASS = {
    "SUCCEEDED": "confirmed_success",
    "FAILED_CONFIRMED": "confirmed_failure",
    "ESCALATED_UNRESOLVED": "escalated_unresolved",
    "CANCELLED_BEFORE_CLAIM": "cancelled",
}


def payload_for(connector, operation, *, changed=False):
    amount = operation.amount_minor + (1 if changed else 0)
    if connector == "payments":
        return {"charge_id": operation.resource_id, "amount_minor": amount}
    if connector == "crm":
        return {"case_id": operation.resource_id, "sku": f"sku_{amount}"}
    return {"customer_id": operation.resource_id, "template": f"t_{amount}"}


class SafeActArm(SafeActBoundaries, SafeActWorkflows, Arm):
    name = "safeact"
    http_trace_enabled = True
    database_trace_enabled = True
    execution_trace_enabled = True
    broker_trace_enabled = True

    def setup(self, slot, workdir):
        self.slot = slot
        self._database_samples = []
        self._capture_leases = False
        self._capture_recovery = False
        self._broker_ids_lock = threading.Lock()
        self._broker_ids = set()
        self._workflow_spec = None
        self._workflow_ids = set()
        self._workflow_observation_errors = []
        self._broker_loss = None
        self.admin = psycopg.connect(**local_admin_settings(), autocommit=True)
        self.env = Environment.disposable(uuid.uuid4().hex)
        self.role_passwords, self.account_passwords = rebuild(
            self.admin, self.env, profiles=QUAL_PROFILES,
            accounts=DEMO_ACCOUNTS)
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        self.trace_dir = workdir / "http-trace"
        self.trace_dir.mkdir(mode=0o700)
        self.execution_dir = workdir / "execution-trace"
        self.execution_dir.mkdir(mode=0o700)
        self.broker_dir = workdir / "broker-trace"
        self.broker_dir.mkdir(mode=0o700)
        credentials = workdir / "safeact-credentials"
        values = credential_lines(self.env, self.role_passwords)
        if slot.private_broker is not None:
            values.update(slot.private_broker.credentials())
        write_credentials(credentials, values)
        self.engine = reset_engine_sessions(sa.create_engine(
            local_database_url(), pool_size=4, max_overflow=0, pool_timeout=5,
            connect_args={"options": f"-c search_path={self.env.schema},public"},
        ))
        self.stack = ProcessStack(
            credentials=credentials, redis_db=slot.redis_db,
            queue=f"safeact-q{slot.index}-{uuid.uuid4().hex[:6]}",
            provider_ports=slot.proxy_ports, poll_seconds=0.2, concurrency=2,
            components=("worker", "relay", "reconciler", "advancer"),
            log_dir=workdir / "safeact-logs",
            extra_env={
                "SAFEACT_STACK_LEASE_SECONDS": str(LEASE_SECONDS),
                "SAFEACT_STACK_VISIBILITY_TIMEOUT": "12",
                "SAFEACT_STACK_FAULT_DIR": str(slot.fault_dir),
                "SAFEACT_STACK_HTTP_TRACE_DIR": str(self.trace_dir),
                "SAFEACT_STACK_EXEC_TRACE_DIR": str(self.execution_dir),
                "SAFEACT_STACK_BROKER_TRACE_DIR": str(self.broker_dir),
            },
        )
        self.stack.start()
        self._warm_up()
        return self

    def _warm_up(self, timeout=60):
        """Every process is up before any scenario is timed."""
        from chaos.scenarios import OperationSpec, Scenario

        probe = Scenario("WARMUP", "warmup", "CONTROL", "payments", "q-v2",
                         0, (), (), True, 5.0)
        answer = self.submit(probe, 0, OperationSpec(
            f"ch_warmup_{uuid.uuid4().hex[:8]}", 100))
        deadline = time.time() + timeout
        while time.time() < deadline:
            view = self.observe([answer.local_id])[answer.local_id]
            if view.outcome_class == "confirmed_success":
                return
            time.sleep(0.2)
        raise RuntimeError("SafeAct arm did not complete its warm-up refund")

    def teardown(self):
        try:
            if getattr(self, "stack", None) is not None:
                self.quiesce()
                problems = self.trace_errors()
                if problems:
                    raise RuntimeError("; ".join(problems))
        finally:
            try:
                if getattr(self, "engine", None) is not None:
                    self.engine.dispose()
            finally:
                if getattr(self, "admin", None) is not None:
                    try:
                        if getattr(self, "env", None) is not None:
                            self.admin.execute(
                                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                "WHERE usename = ANY(%s)", (list(self.env.roles),))
                            with self.admin.transaction():
                                for statement in teardown_statements(self.admin, self.env):
                                    self.admin.execute(statement)
                    finally:
                        self.admin.close()

    def quiesce(self):
        if not getattr(self, "_quiesced", False):
            if self._broker_loss is not None:
                self._broker_loss.resume()
            self._stopped_processes = stop_processes(self.stack)
            self._quiesced = True
        return self._stopped_processes

    def prepare(self, scenario):
        self._workflow_spec = scenario.workflow
        self._capture_leases = "F06" in scenario.family.split("+")
        self._capture_recovery = bool(set(scenario.family.split("+")) & {"F07", "F09", "F16"})
        self._broker_since_ns = time.monotonic_ns()
        with self._broker_ids_lock:
            self._broker_ids.clear()
            self._workflow_ids.clear()
        for component in ("worker", "reconciler", "relay", "advancer"):
            process = self.stack.processes.get(component)
            if process is not None and process.poll() is not None:
                self.stack.spawn(component)          # a supervisor restart
        self.prepare_boundary(scenario)
        time.sleep(0.2)

    def kill_parked_worker(self, point, emit):
        kill_parked_worker(self.stack, self.slot.fault_dir, point, emit)

    def execution_observations(self, since_ns):
        return read_execution(self.execution_dir, since_ns)

    def broker_observations(self, since_ns):
        return read_broker(self.broker_dir, since_ns)

    def _broker_admitted(self):
        with self._broker_ids_lock:
            return set(self._broker_ids)

    def begin_broker_loss(self, emit, *, kill=False):
        if getattr(self.slot, "_lock_fd", None) is None:
            raise RuntimeError("Broker loss requires ownership of the qualification slot lock")
        kind = CrashedBrokerLoss if kill else QueuedLoss
        options = {"broker": self.slot.private_broker} if kill else {}
        self._broker_loss = kind(self.stack, self.slot.redis(), self.slot.redis_db,
            self.stack.env["SAFEACT_STACK_QUEUE"], self._broker_admitted, self._broker_unclaimed,
            lambda: self.broker_observations(self._broker_since_ns), emit, **options)
        self._broker_loss.begin()

    def finish_broker_loss(self, cancelled):
        self._broker_loss.finish(cancelled)

    def redeliver_during_dispatch(self, held, cancelled, emit):
        if getattr(self.slot, "_lock_fd", None) is None:
            raise RuntimeError("Redelivery requires ownership of the qualification slot lock")
        driver = DispatchRedelivery(self.slot.redis(), self.slot.redis_db,
            self.stack.env["SAFEACT_STACK_QUEUE"], self._broker_admitted,
            self._broker_dispatching, lambda: self.broker_observations(self._broker_since_ns),
            lambda tag: restore_unacked(self.slot.redis_url, tag), emit)
        driver.run(held, cancelled)

    def _broker_dispatching(self, message):
        started = time.monotonic_ns()
        s = sql.Identifier(self.env.schema)
        with psycopg.connect(**local_admin_settings(), autocommit=True) as connection:
            rows = connection.execute(sql.SQL(
                "SELECT p.state::text, p.operation_id, p.seq, p.claim_id, p.attempt_id, "
                "extract(epoch FROM p.lease_expires_at), "
                "p.lease_expires_at > clock_timestamp(), o.connector_id, "
                "coalesce(o.provider_idempotency_key, o.operation_id::text) "
                "FROM {}.safeact_state_projection p "
                "JOIN {}.safeact_operations o USING (operation_id) "
                "JOIN {}.safeact_outbox_deliveries d USING (operation_id) "
                "WHERE p.operation_id = %s AND d.outbox_id = %s AND d.delivery_id = %s"
            ).format(s, s, s), (message["operation_id"], message["outbox_id"], message["delivery_id"])).fetchall()
        finished = time.monotonic_ns()
        if len(rows) != 1 or rows[0][0] != "DISPATCHING" or rows[0][6] is not True:
            raise RuntimeError("Original dispatch fence and live lease were not independently observed")
        sample = lease_rows([rows[0][1:]], started, finished)[0]
        sample.update(message, event="db_broker_dispatching", state="DISPATCHING")
        self._database_samples.append(sample)

    def _broker_unclaimed(self, messages):
        started = time.monotonic_ns()
        with psycopg.connect(**local_admin_settings(), autocommit=True) as connection:
            connection.execute("SET statement_timeout = '2s'")
            rows = connection.execute(sql.SQL(
                "SELECT d.operation_id, d.outbox_id, d.delivery_id, p.state::text, p.seq "
                "FROM {}.safeact_outbox_deliveries d "
                "JOIN {}.safeact_state_projection p USING (operation_id) "
                "WHERE d.delivery_id = ANY(%s::uuid[])"
            ).format(sql.Identifier(self.env.schema), sql.Identifier(self.env.schema)),
                ([m["delivery_id"] for m in messages],)).fetchall()
        finished = time.monotonic_ns()
        expected = {(m["operation_id"], m["outbox_id"], m["delivery_id"]) for m in messages}
        actual = {(str(op), str(outbox), str(delivery)) for op, outbox, delivery, state, seq in rows if state == "RECORDED"}
        if expected != actual:
            raise RuntimeError("Queued scenario work is not independently persisted and unclaimed")
        self._database_samples.extend(dict(source="database", event="db_broker_pending",
            operation_id=str(op), outbox_id=str(outbox), delivery_id=str(delivery), state=state,
            transition_seq=seq, sample_started_ns=started, sample_finished_ns=finished,
            monotonic_ns=finished) for op, outbox, delivery, state, seq in rows)

    def account(self, scenario):
        return f"acct-{scenario.scenario_id.lower()}"

    def submit(self, scenario, index, operation, *, changed=False):
        if scenario.family in ("F14", "F15"):
            return self.boundary_submit(scenario, index, operation, changed=changed)
        if scenario.workflow is not None:
            return self.workflow_submit(scenario, index, operation, changed=changed)
        connector_id, action = CONNECTORS[scenario.connector]
        request_key = f"{scenario.scenario_id}-{index}"
        account = self.account(scenario)
        if scenario.connector == "payments":
            self.slot.mocks["payments"].register_key(derive_provider_key(
                TENANT, request_key, connector_id, account), scenario.contract_version)
        try:
            receipt = submit_intent(self.engine, IntentSubmission(
                tenant_id=TENANT, actor_id="qualification",
                request_key=request_key, action_type=action,
                resource_id=operation.resource_id,
                provider_account_id=account, connector_id=connector_id,
                contract_version=scenario.contract_version,
                payload=payload_for(scenario.connector, operation,
                                    changed=changed),
            ))
        except SubmissionConflict:
            return Admission(None, "conflict")
        if scenario.scenario_id != "WARMUP":
            with self._broker_ids_lock:
                self._broker_ids.add(str(receipt.operation_id))
        return Admission(str(receipt.operation_id))

    def observe(self, local_ids):
        if self._workflow_spec is not None:
            return self.workflow_observe(local_ids)
        if self._capture_leases and local_ids:
            self._sample_leases(local_ids)
        if self._capture_recovery and local_ids:
            self._sample_recovery(self.admin, local_ids)
            self._database_samples.extend(sample_breakers(self.admin, self.env.schema, local_ids))
        s = sql.Identifier(self.env.schema)
        views = {}
        for local_id in local_ids:
            state = self.admin.execute(sql.SQL(
                "SELECT state FROM {}.safeact_state_projection "
                "WHERE operation_id = %s").format(s), (local_id,)).fetchone()
            transitions = self.admin.execute(sql.SQL(
                "SELECT to_state, reason, extract(epoch FROM created_at) "
                "FROM {}.safeact_operation_transitions "
                "WHERE operation_id = %s ORDER BY seq").format(s),
                (local_id,)).fetchall()
            evidence = self.admin.execute(sql.SQL(
                "SELECT count(*) FROM {}.safeact_attempt_outcomes "
                "WHERE operation_id = %s").format(s), (local_id,)).fetchone()[0]
            current = state[0] if state else "MISSING"
            view = OperationView(
                local_id=local_id,
                outcome_class=CLASS.get(current, "pending"),
                state=current,
                events=[(float(at), f"{to}:{reason}")
                        for to, reason, at in transitions],
                evidence_rows=evidence,
            )
            for to, reason, at in transitions:
                if to in ("SUCCEEDED", "FAILED_CONFIRMED") and (
                        view.resolved_at is None):
                    view.resolved_at = float(at)
                if to == "ESCALATED_UNRESOLVED" and view.escalated_at is None:
                    view.escalated_at = float(at)
            views[local_id] = view
        return views

    def _sample_leases(self, local_ids):
        started = time.monotonic_ns()
        rows = self.admin.execute(sql.SQL(
            "SELECT t.operation_id, t.seq, t.claim_id, t.attempt_id, "
            "extract(epoch FROM t.lease_expires_at), "
            "t.lease_expires_at > clock_timestamp(), o.connector_id, "
            "coalesce(o.provider_idempotency_key, o.operation_id::text) "
            "FROM {}.safeact_operation_transitions t "
            "JOIN {}.safeact_operations o USING (operation_id) "
            "WHERE t.operation_id = ANY(%s::uuid[]) "
            "AND t.to_state = 'DISPATCHING' AND t.lease_expires_at IS NOT NULL "
            "ORDER BY t.operation_id, t.seq"
        ).format(sql.Identifier(self.env.schema), sql.Identifier(self.env.schema)),
            (local_ids,)).fetchall()
        self._database_samples.extend(lease_rows(rows, started, time.monotonic_ns()))

    def _sample_recovery(self, connection, local_ids):
        samples = sample_recovery(connection, self.env.schema, local_ids)
        self._database_samples.extend(samples)
        return samples

    def release_late_commit(self, held, cancelled, emit):
        settings = dict(local_admin_settings(), connect_timeout=2)
        with psycopg.connect(**settings, autocommit=True) as connection:
            connection.execute("SET statement_timeout = '2s'")
            def sample():
                with self._broker_ids_lock:
                    ids = sorted(self._broker_ids)
                return self._sample_recovery(connection, ids) if ids else []
            return release_after_escalation(held, cancelled, sample, emit)

    def database_observations(self, local_ids):
        local_ids = self.execution_ids(local_ids)
        if not local_ids:
            return list(self._database_samples)
        started = time.monotonic_ns()
        rows = self.admin.execute(sql.SQL(
            "SELECT audit_id, operation_id, event_type, commit_xid::text, "
            "extract(epoch FROM created_at) FROM {}.safeact_audit_events "
            "WHERE operation_id = ANY(%s::uuid[]) ORDER BY audit_id"
        ).format(sql.Identifier(self.env.schema)), (local_ids,)).fetchall()
        return list(self._database_samples) + audit_rows(rows, started, time.monotonic_ns())

    def check_integrity(self):
        return [f"projection drift: {d.operation_id}"
                for d in projection_drift(self.admin, self.env.schema)] + self.trace_errors()

    def trace_observations(self, since_ns):
        return read_trace(self.trace_dir, since_ns)

    def trace_errors(self):
        errors = [f"Workflow DB observer failed: {kind}"
                  for kind in self._workflow_observation_errors]
        directory = getattr(getattr(self, "stack", None), "log_dir", None)
        if directory is None:
            return errors
        return errors + [f"Qualification observation failed: {path.name}" for path in directory.glob("*.log")
                if any(marker.strip() in path.read_bytes() for marker in (TRACE_ERROR, EXEC_TRACE_ERROR, BROKER_TRACE_ERROR))]
