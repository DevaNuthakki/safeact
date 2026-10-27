"""Evaluation harness: environments, trial setup and ground truth.

Guarded arm (and the deterministic caller): one disposable
``safeact_it_<hex>`` environment per run with the evaluation contracts
(profiles.py), the whole stack (API, guarded MCP server, relay, Celery
worker on Redis database 12, reconciler, advancer) and scripted mock
providers driven by a shared FaultPlan. Each trial gets a fresh service
principal, its own key and exact grants for a fresh, seeded target, so
trials never share state.

Direct arm: fresh mock providers, ledger and fault plan per trial, served
by mcp_server.direct over the SDK's in-memory transport.

Ground truth is read from the harness effects ledger (applied effects
per business resource) and, for the guarded arm, the database. A
caller's report is never evidence.
"""

import hashlib
import json
import socket
import time
import uuid

from dataclasses import dataclass, field

import psycopg
import redis
from psycopg import sql

from connectors.ledger import EffectsLedger
from deploy.demo.bootstrap import Environment, rebuild, teardown_statements
from deploy.demo.run_stack import Stack
from deploy.evaluation.agent_v1.faults import (
    EvalCrm, EvalEmail, EvalPayments, Fault, FaultPlan, calls_by_resource,
)
from deploy.evaluation.agent_v1.profiles import AGENT_PROFILES
from deploy.evaluation.agent_v1.scenarios import ACTIONS
from mcp_server.direct.server import build_server as build_direct_server
from storage import agent_api
from storage.database import local_admin_settings, local_redis_url
from storage.identity_admin import create_principal, issue_agent_key
from storage.state_projection import assert_no_drift


REDIS_DB = 12
TENANT = "tenant-eval-north"
SETTLED = ("SUCCEEDED", "FAILED_CONFIRMED", "CANCELLED_BEFORE_CLAIM",
           "ESCALATED_UNRESOLVED", "VERIFYING", "UNKNOWN")
MOVING = ("RECORDED", "CLAIMED", "DISPATCHING", "RETRY_ELIGIBLE")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def seeded_hex(*parts, length=10):
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:length]


@dataclass
class Trial:
    """Everything a caller is told, and what the harness keeps to itself."""

    arm: str
    scenario: dict
    index: int
    target: str
    intent_ref: str
    prior_ref: str | None
    payload: dict
    task: str
    fault: Fault
    key: str | None = None                       # guarded arm only
    direct_server: object = None                 # direct arm only
    ledger: object = None
    plan: object = None
    extra: dict = field(default_factory=dict)


def make_trial(arm, scenario, index, run_id):
    connector, account, resource_field, prefix = ACTIONS[scenario["action"]]
    region = "north" if scenario["granted"] else "south"
    suffix = seeded_hex(run_id, scenario["seed"], arm, index)
    target = f"{prefix}_{region}_{suffix}"
    intent_ref = "task-" + seeded_hex(run_id, scenario["seed"], arm, index, "intent", length=8)
    prior_ref = None
    if scenario["setup"] == "prior_attempt":
        prior_ref = "prior-" + seeded_hex(run_id, scenario["seed"], arm, index, "prior", length=8)
    payload = {resource_field: target, **scenario["payload"]}
    fault = Fault(**scenario["fault"])
    return Trial(arm=arm, scenario=scenario, index=index, target=target,
                 intent_ref=intent_ref, prior_ref=prior_ref, payload=payload,
                 task="", fault=fault)


def render_task(trial):
    """The task text, after setup (it may name the prior attempt)."""

    scenario = trial.scenario
    _, account, _, _ = ACTIONS[scenario["action"]]
    prior = ""
    if trial.prior_ref and trial.arm.startswith("direct"):
        prior = f"idempotency key {trial.prior_ref}"
    elif trial.prior_ref:
        prior = (f"SafeAct operation {trial.extra['prior_operation_id']}, "
                 f"intent reference {trial.prior_ref}")
    trial.task = scenario["task"].format(
        resource=trial.target, account=account, intent_ref=trial.intent_ref,
        prior=prior,
        amount=scenario["payload"].get("amount_minor", ""),
        sku=scenario["payload"].get("sku", ""),
        template=scenario["payload"].get("template", ""),
    )
    return trial


def _canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class GuardedEnvironment:
    """The SafeAct stack the guarded agent and deterministic caller use."""

    def __init__(self, *, poll_seconds=0.2):
        self.poll = poll_seconds
        self.env = Environment.disposable(uuid.uuid4().hex)
        self.admin = None
        self.stack = None
        self.plan = FaultPlan()
        self.ledger = EffectsLedger()

    @property
    def mcp_url(self):
        return self.stack.mcp_url

    def start(self):
        redis.Redis.from_url(local_redis_url(REDIS_DB)).flushdb()
        self.admin = psycopg.connect(**local_admin_settings(), autocommit=True)
        role_passwords, _ = rebuild(
            self.admin, self.env, profiles=AGENT_PROFILES, accounts=()
        )
        providers = (
            EvalPayments(ledger=self.ledger, plan=self.plan),
            EvalCrm(ledger=self.ledger, plan=self.plan),
            EvalEmail(ledger=self.ledger, plan=self.plan),
        )
        env = self.env
        self.stack = Stack(
            schema=env.schema,
            roles={"api": env.api, "worker": env.worker,
                   "recoverer": env.recoverer, "advancer": env.advancer,
                   "mcp": env.mcp},
            passwords=role_passwords, port=free_port(), redis_db=REDIS_DB,
            poll_seconds=self.poll, mcp_port=free_port(),
            ledger=self.ledger, providers=providers,
        ).start()
        return self

    def stop(self):
        self.plan.cancel_timers()
        if self.stack is not None:
            self.stack.stop()
        if self.admin is not None:
            try:
                # A trial must leave the projection equal to the log.
                assert_no_drift(self.admin, self.env.schema)
            finally:
                self._teardown()

    def _teardown(self):
        try:
            self.admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE usename = ANY(%s)", (list(self.env.roles),),
            )
            with self.admin.transaction():
                for statement in teardown_statements(self.admin, self.env):
                    self.admin.execute(statement)
        finally:
            self.admin.close()

    def _table(self, name):
        return sql.SQL("{}.{}").format(sql.Identifier(self.env.schema),
                                      sql.Identifier(name))

    def prepare(self, trial):
        """A fresh agent with exact grants (when granted) and its key."""

        connector, account, _, _ = ACTIONS[trial.scenario["action"]]
        grants = ()
        if trial.scenario["granted"]:
            # The scenario's contract, plus the default v1 a caller would
            # use for a new request on the same target.
            versions = sorted({trial.scenario["contract_version"], "v1"})
            grants = tuple(
                (connector, account, trial.scenario["action"], trial.target, v)
                for v in versions
            )
        self.plan.set(trial.target, trial.fault)
        with self.admin.transaction():
            principal = create_principal(
                self.admin, self.env.schema, tenant_id=TENANT,
                username="eval-agent-" + uuid.uuid4().hex[:12],
                display_name="Evaluation agent", kind="service",
                roles=("investigator", "requester"), grants=grants,
            )
            trial.key = issue_agent_key(
                self.admin, self.env.schema, principal_id=principal,
                scopes=("investigate", "submit"), label="eval",
                created_by="agent-eval-harness",
            ).key
        trial.ledger, trial.plan = self.ledger, self.plan
        trial.extra["principal_id"] = str(principal)
        if trial.prior_ref:
            self._prior_attempt(trial)
        trial.extra["effects_before"] = self.plan.effects()
        return render_task(trial)

    def _prior_attempt(self, trial):
        """Submit the earlier attempt and wait until its key has expired."""

        connector, account, _, _ = ACTIONS[trial.scenario["action"]]
        started = time.monotonic()
        with psycopg.connect(**self.stack.mcp_settings.connect_kwargs(),
                             autocommit=True) as connection:
            admitted = agent_api.submit(
                connection, self.env.schema, key=trial.key,
                intent_ref=trial.prior_ref,
                action_type=trial.scenario["action"],
                resource_id=trial.target, provider_account_id=account,
                connector_id=connector,
                contract_version=trial.scenario["contract_version"],
                payload=_canonical(trial.payload),
            )
        trial.extra["prior_operation_id"] = str(admitted.operation_id)
        retention = trial.fault.retention_seconds or 0
        deadline = started + 60
        while time.monotonic() < deadline:
            states = self.states(trial.target)
            if (states and states[0][0] == "ESCALATED_UNRESOLVED"
                    and time.monotonic() - started > retention + 1.5):
                return
            time.sleep(0.3)
        raise RuntimeError("prior attempt did not escalate in time")

    def states(self, target):
        return [tuple(row) for row in self.admin.execute(sql.SQL(
            "SELECT c.state, t.reason FROM {} o JOIN {} c "
            "ON c.operation_id = o.operation_id JOIN {} t "
            "ON t.operation_id = o.operation_id AND t.seq = c.seq "
            "WHERE o.resource_id = %s ORDER BY o.created_at"
        ).format(self._table("safeact_operations"),
                 self._table("safeact_operation_current_state"),
                 self._table("safeact_operation_transitions")),
            (target,)).fetchall()]

    def settle(self, trial, limit=20):
        """Wait until nothing on the target is still moving (bounded)."""

        deadline = time.monotonic() + limit
        delay = trial.fault.delay_seconds or 0
        time.sleep(min(delay, limit) if trial.fault.kind == "pending" else 0)
        while time.monotonic() < deadline:
            if not any(state in MOVING for state, _ in self.states(trial.target)):
                break
            time.sleep(0.3)
        time.sleep(1.0)

    def effects(self, trial):
        return self.plan.effects().get(trial.target, 0)

    def orphan_effects(self, trial):
        """Orphan effects (blueprint 09): effects applied during the trial on
        any resource but its target, i.e. with no requested operation."""

        before, after = trial.extra.get("effects_before", {}), self.plan.effects()
        return {r: n - before.get(r, 0) for r, n in after.items()
                if r != trial.target and n > before.get(r, 0)}

    def provider_calls(self, trial):
        return calls_by_resource(self.ledger).get(trial.target, 0)

    def reasons(self, target):
        """Distinct transition reasons on the target, in order (evidence)."""

        rows = self.admin.execute(sql.SQL(
            "SELECT t.reason FROM {} o JOIN {} t "
            "ON t.operation_id = o.operation_id "
            "WHERE o.resource_id = %s ORDER BY o.created_at, t.seq"
        ).format(self._table("safeact_operations"),
                 self._table("safeact_operation_transitions")),
            (target,)).fetchall()
        return list(dict.fromkeys(row[0] for row in rows))


class DirectProviders:
    """Fresh mock providers for one direct-arm trial."""

    def prepare(self, trial):
        trial.ledger, trial.plan = EffectsLedger(), FaultPlan()
        trial.plan.set(trial.target, trial.fault)
        payments = EvalPayments(ledger=trial.ledger, plan=trial.plan)
        crm = EvalCrm(ledger=trial.ledger, plan=trial.plan)
        email = EvalEmail(ledger=trial.ledger, plan=trial.plan)
        trial.extra["providers"] = (payments, crm, email)
        trial.direct_server = build_direct_server(
            payments=payments, crm=crm, email=email,
        )
        if trial.prior_ref:
            from connectors.payments_mock import ProviderTimeout
            try:
                payments.refund(trial.prior_ref, dict(trial.payload), timeout=2.0)
            except ProviderTimeout:
                pass
            time.sleep((trial.fault.retention_seconds or 0) + 1.5)
        trial.extra["effects_before"] = trial.plan.effects()
        return render_task(trial)

    def settle(self, trial, limit=20):
        if trial.fault.kind in ("pending", "stale_index") and trial.fault.delay_seconds:
            time.sleep(min(trial.fault.delay_seconds, limit))
        time.sleep(0.5)
        trial.plan.cancel_timers()

    def states(self, target):
        return []

    def reasons(self, target):
        return []

    def effects(self, trial):
        return trial.plan.effects().get(trial.target, 0)

    def orphan_effects(self, trial):
        before, after = trial.extra.get("effects_before", {}), trial.plan.effects()
        return {r: n - before.get(r, 0) for r, n in after.items()
                if r != trial.target and n > before.get(r, 0)}

    def provider_calls(self, trial):
        return calls_by_resource(trial.ledger).get(trial.target, 0)
