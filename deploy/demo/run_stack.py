"""Run the whole local stack against one environment (DEMO ONLY).

    .venv/bin/python -m deploy.demo.bootstrap      # once, or to reset
    .venv/bin/python -m deploy.demo.run_stack       # API :8000, MCP :8002
    .venv/bin/python -m deploy.demo.run_stack --processes
                                                     # same, one OS process
                                                     # per component, HTTP
                                                     # mock providers

One process runs, each as its own database role:
- the API (uvicorn) as the API role,
- the guarded MCP server (Streamable HTTP, 127.0.0.1) as the MCP role,
  for agents holding a key from .demo-credentials,
- the relay, publishing the durable outbox to Celery over Redis,
- a Celery worker (in-process) executing claims as the worker role,
- the reconciler as the recovery role,
- the workflow advancer as the advancer role,
against in-process mock providers. Nothing reaches a real provider.

Scripted faults are harness-only: a few synthetic resource ids make the
mock providers misbehave in a fixed way, so the walkthrough can show
uncertainty. The API never sees or chooses them; they come from this
file, keyed by the payload a requester was granted to submit.

  ch_<tenant>_1002   payments: effect applied, response lost
                     (timeout after success) -> verified by lookup
  case_<tenant>_2002 CRM: effect applied, response lost, record not yet
                     searchable (late commit, empty lookup) -> escalates
  case_<tenant>_2003 CRM: definitive rejection before execution, may be
                     retried -> waits for a reviewer's PROTECTED_RETRY
  case_<tenant>_2004 CRM: permanent rejection before execution -> fixed
                     workflow refunds the confirmed shipping charge
  cust_<tenant>_3002 email: sent, response lost -> escalates at once
                     (no lookup, never resent)

Everything else succeeds normally.

    .venv/bin/python -m deploy.demo.run_stack --stripe

adds the optional Stripe test-mode demo profile (deploy/demo/stripe_demo.py):
a console-only refund of a Stripe test PaymentIntent, through the real adapter
and its gates (rk_test_ key file, SAFEACT_ALLOW_STRIPE_SANDBOX=1, never in CI;
any live-mode object stops the stack). Without --stripe nothing reaches a real
provider.
"""

import argparse
import logging
import re
import signal
import threading
import time
import uuid

from pathlib import Path

import psycopg
import uvicorn
from celery.contrib.testing.worker import start_worker

from api import logging as api_logging
from api.app import create_app
from api.settings import Settings
from connectors.crm import CrmConnector
from connectors.crm_mock import MockCrmProvider
from connectors.email import EmailConnector
from connectors.email_mock import MockEmailProvider
from connectors.ledger import EffectsLedger
from connectors.payments import PaymentsConnector
from connectors.payments_mock import MockPaymentsProvider
from mcp_server.server import create_app as create_mcp_app
from observability import metrics
from mcp_server.settings import McpSettings
from storage.database import local_admin_settings, local_redis_url
from storage.connection_pool import ConnectionPool
from storage.experimental_celery import CeleryPublisher, make_app
from storage.experimental_durable_execution import ExecutionWorker, relay_once
from storage.experimental_recovery_worker import Reconciler
from storage.experimental_workflow import WorkflowAdvancer


log = logging.getLogger("safeact.stack")

ROOT = Path(__file__).resolve().parents[2]

SCRIPT = (
    (re.compile(r"^ch_[a-z]+_1002$"), "payments", "timeout_after_success"),
    (re.compile(r"^case_[a-z]+_2002$"), "crm", "late_commit_unsearchable"),
    (re.compile(r"^case_[a-z]+_2003$"), "crm", "rejected_retryable"),
    (re.compile(r"^case_[a-z]+_2004$"), "crm", "rejected_permanent"),
    (re.compile(r"^cust_[a-z]+_3002$"), "email", "sent_response_lost"),
)


def scripted(provider_kind, params):
    resource = (
        params.get("charge_id") or params.get("case_id")
        or params.get("customer_id") or ""
    )
    for pattern, kind, scenario in SCRIPT:
        if kind == provider_kind and pattern.match(str(resource)):
            return scenario
    return None


class ScriptedPayments(MockPaymentsProvider):
    def _request(self, prefix, idempotency_key, params, timeout):
        # Only the first request for a key misbehaves; a same-key retry
        # or lookup sees the provider's honest state.
        if (
            scripted("payments", params) == "timeout_after_success"
            and not self._ledger.calls_for(idempotency_key)
        ):
            self.fail_after_effect = True
        return super()._request(prefix, idempotency_key, params, timeout)


class ScriptedCrm(MockCrmProvider):
    def create_record(self, external_ref, params, *, timeout=None):
        if not self._ledger.calls_for(external_ref):
            scenario = scripted("crm", params)
            if scenario == "late_commit_unsearchable":
                self.fail_after_effect = True
                self.drop_from_index = True
            elif scenario == "rejected_retryable":
                self.reject_next_retryable = "crm-validation-hold"
            elif scenario == "rejected_permanent":
                self.reject_next = "crm-replacement-unavailable"
        return super().create_record(external_ref, params, timeout=timeout)


class ScriptedEmail(MockEmailProvider):
    def send(self, message_id, params, *, timeout=None):
        if (
            scripted("email", params) == "sent_response_lost"
            and not self._ledger.calls_for(message_id)
        ):
            self.fail_after_effect = True
        return super().send(message_id, params, timeout=timeout)


class Loop(threading.Thread):
    """Call ``step`` every ``interval`` seconds until stopped."""

    def __init__(self, name, step, interval):
        super().__init__(name=name, daemon=True)
        self._step = step
        self._interval = interval
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                self._step()
            except Exception as error:  # keep the loop alive; log it
                metrics.LOOP_ERRORS.labels(self.name).inc()
                log.warning("loop_error", extra={
                    "loop": self.name, "error_type": type(error).__name__,
                })
            self._stop.wait(self._interval)

    def stop(self):
        self._stop.set()


class Stack:
    """API (+ MCP) + relay + Celery worker + reconciler + advancer."""

    def __init__(self, *, schema, roles, passwords, port=8000,
                 redis_db=1, poll_seconds=0.5, sse=None, mcp_port=None,
                 ledger=None, providers=None, extra_connectors=()):
        self.schema = schema
        self.roles = roles
        self.passwords = passwords
        self.port = port
        self.redis_url = local_redis_url(redis_db)
        self.poll = poll_seconds
        # The evaluation harness passes its own scripted providers (and
        # their ledger); otherwise the demo's fixed fault script applies.
        self.ledger = ledger or EffectsLedger()
        if providers is None:
            providers = (
                ScriptedPayments(ledger=self.ledger),
                ScriptedCrm(ledger=self.ledger),
                ScriptedEmail(ledger=self.ledger),
            )
        self.payments, self.crm, self.email = providers
        # extra_connectors: the optional Stripe test-mode demo profile.
        self.connectors = (
            PaymentsConnector(self.payments), CrmConnector(self.crm),
            EmailConnector(self.email),
        ) + tuple(extra_connectors)
        admin = local_admin_settings()
        self.settings = Settings(
            schema=schema, role=roles["api"], password=passwords[roles["api"]],
            host=admin["host"], port=admin["port"], dbname=admin["dbname"],
            **(sse or {}),
        )
        self.mcp_port = mcp_port
        self.mcp_settings = None
        if mcp_port is not None:
            self.mcp_settings = McpSettings(
                schema=schema, role=roles["mcp"],
                password=passwords[roles["mcp"]],
                host=admin["host"], port=admin["port"], dbname=admin["dbname"],
            )
        self._threads = []
        self._mcp = None
        self._worker = None
        self._pools = {}

    def connect(self, component):
        settings = local_admin_settings()
        role = self.roles[component]
        settings.update(user=role, password=self.passwords[role])
        return psycopg.connect(**settings)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def mcp_url(self):
        return f"http://127.0.0.1:{self.mcp_port}/mcp"

    def start(self):
        # A start that fails half way must not leave a Celery worker or a
        # server thread running in this process: the next stack would
        # share its kombu event loop ("concurrent poll() invocation").
        try:
            return self._start()
        except BaseException:
            self.stop()
            raise

    def _start(self):
        for component in ("worker", "recoverer", "advancer"):
            settings = local_admin_settings()
            role = self.roles[component]
            settings.update(user=role, password=self.passwords[role])
            # Worker and relay share this role but may run simultaneously.
            self._pools[component] = ConnectionPool(settings, size=2)
        queue = "safeact-stack-" + uuid.uuid4().hex[:8]
        celery = make_app(self.redis_url, queue=queue, visibility_timeout=120)
        celery.safeact_execute = ExecutionWorker(
            connect=lambda: self.connect("worker"), schema=self.schema,
            connection_scope=self._pools["worker"].connection,
            connectors=self.connectors, lease_seconds=30,
        ).execute
        publisher = CeleryPublisher(celery, queue=queue)
        self._worker = start_worker(
            celery, pool="solo", concurrency=1, perform_ping_check=False,
            shutdown_timeout=10, loglevel="WARNING",
        )
        self._worker.__enter__()

        def relay():
            with self._pools["worker"].connection() as connection:
                relay_once(connection, self.schema, publisher)

        reconciler = Reconciler(
            connect=lambda: self.connect("recoverer"), schema=self.schema,
            connection_scope=self._pools["recoverer"].connection,
            connectors=self.connectors, retry_authorized=True,
        )
        advancer = WorkflowAdvancer(
            connect=lambda: self.connect("advancer"), schema=self.schema,
            connection_scope=self._pools["advancer"].connection,
        )

        # log_config=None: an embedded server must not run dictConfig.
        # It flushes every registered handler, and a handler another
        # thread (the Celery worker) is still constructing has no stream
        # yet, so start-up failed intermittently (M5.3, two of three
        # agent-harness runs on 2026-10-21). It also kept replacing the
        # JSON logging this process configured.
        self._server = uvicorn.Server(uvicorn.Config(
            create_app(self.settings), host="127.0.0.1", port=self.port,
            log_level="warning", log_config=None,
        ))
        self._threads = [
            threading.Thread(target=self._server.run, name="api", daemon=True),
            Loop("relay", relay, self.poll),
            Loop("reconciler", reconciler.reconcile_once, self.poll),
            Loop("advancer", advancer.advance_once, self.poll),
        ]
        servers = [self._server]
        if self.mcp_settings is not None:
            # The MCP app needs its lifespan (the session manager).
            self._mcp = uvicorn.Server(uvicorn.Config(
                create_mcp_app(self.mcp_settings), host="127.0.0.1",
                port=self.mcp_port, log_level="warning", access_log=False,
                log_config=None,
            ))
            self._threads.append(threading.Thread(
                target=self._mcp.run, name="mcp", daemon=True,
            ))
            servers.append(self._mcp)
        for thread in self._threads:
            thread.start()
        deadline = time.monotonic() + 15
        while not all(server.started for server in servers):
            if time.monotonic() > deadline:
                raise RuntimeError("API or MCP server did not start")
            time.sleep(0.05)
        return self

    def stop(self):
        for thread in self._threads:
            if isinstance(thread, Loop):
                thread.stop()
        for server in (getattr(self, "_server", None), self._mcp):
            if server is not None:
                server.should_exit = True
        for thread in self._threads:
            if thread.ident is not None:  # never started: nothing to join
                thread.join(timeout=15)
        if self._worker is not None:
            self._worker.__exit__(None, None, None)
            self._worker = None
        for pool in self._pools.values():
            pool.close()

    def effects(self, operation_id, provider_key=None):
        """Applied effects the harness ledger counted for one operation."""

        correlation = provider_key or str(operation_id)
        return len(self.ledger.effects_for(correlation))


def demo_stack(port, credentials=None, mcp_port=8002, extra_connectors=()):
    from deploy.demo.bootstrap import CREDENTIALS, read_credentials

    values = read_credentials(credentials or CREDENTIALS)
    roles = {
        "api": values["SAFEACT_DEMO_API_ROLE"],
        "worker": values["SAFEACT_DEMO_WORKER_ROLE"],
        "recoverer": values["SAFEACT_DEMO_RECOVERER_ROLE"],
        "advancer": values["SAFEACT_DEMO_ADVANCER_ROLE"],
        "mcp": values["SAFEACT_DEMO_MCP_ROLE"],
    }
    passwords = {
        roles["api"]: values["SAFEACT_DEMO_API_PASSWORD"],
        roles["worker"]: values["SAFEACT_DEMO_WORKER_PASSWORD"],
        roles["recoverer"]: values["SAFEACT_DEMO_RECOVERER_PASSWORD"],
        roles["advancer"]: values["SAFEACT_DEMO_ADVANCER_PASSWORD"],
        roles["mcp"]: values["SAFEACT_DEMO_MCP_PASSWORD"],
    }
    return Stack(
        schema=values["SAFEACT_DEMO_SCHEMA"], roles=roles,
        passwords=passwords, port=port, mcp_port=mcp_port,
        extra_connectors=extra_connectors,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--mcp-port", type=int, default=8002)
    parser.add_argument(
        "--processes", action="store_true",
        help="one OS process per component (real Celery worker, HTTP mocks)",
    )
    parser.add_argument(
        "--stripe", action="store_true",
        help="add the Stripe test-mode demo profile (needs "
             "SAFEACT_ALLOW_STRIPE_SANDBOX=1, the rk_test_ key file and "
             "bootstrap --stripe; see deploy/demo/stripe_demo.py)",
    )
    args = parser.parse_args(argv)
    if args.stripe and args.processes:
        parser.error("--stripe runs with the in-process stack only")

    api_logging.configure()
    stopping = threading.Event()
    if args.processes:
        from deploy.stack.processes import ProcessStack

        stack = ProcessStack(api_port=args.port, mcp_port=args.mcp_port,
                             log_dir=ROOT / ".stack-logs").start()
        print(
            f"SafeAct demo stack (processes) on {stack.base_url}, MCP on "
            f"http://127.0.0.1:{args.mcp_port}/mcp, mock admin "
            f"{stack.admin_url} (DEMO ONLY). Ctrl-C stops."
        )
    else:
        extra = ()
        if args.stripe:
            from deploy.demo.stripe_demo import demo_connector, read_fixtures

            # Gated as always: refused without SAFEACT_ALLOW_STRIPE_SANDBOX=1
            # or under CI, and any live-mode object stops this stack.
            extra = (demo_connector(read_fixtures(), on_live=stopping.set),)
        stack = demo_stack(args.port, mcp_port=args.mcp_port,
                           extra_connectors=extra).start()
        print(
            f"SafeAct demo stack on {stack.base_url}, MCP on {stack.mcp_url} "
            f"{'with Stripe test mode ' if args.stripe else ''}"
            "(DEMO ONLY). Ctrl-C stops."
        )
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    stopping.wait()
    stack.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
