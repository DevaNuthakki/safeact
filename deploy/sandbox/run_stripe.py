"""Stripe sandbox qualification run (test mode only; DEMO/SANDBOX ONLY).

    # offline, against the documented-behaviour fake (CI and tests):
    .venv/bin/python -m deploy.sandbox.run_stripe --fake --out /tmp/stripe

    # real Stripe sandbox, only after the author created the key file:
    SAFEACT_ALLOW_STRIPE_SANDBOX=1 .venv/bin/python -m deploy.sandbox.run_stripe \
        --manifest deploy/sandbox/manifest_stripe_v1.json --out DIR

Runs the frozen scenarios in deploy/sandbox/manifest_stripe_v1.json in a
disposable schema with the stripe-test contract registered:
- "through" scenarios go through SafeAct itself: admission, relay,
  ExecutionWorker and Reconciler, with the Stripe adapter as the
  connector. A harness wrapper can drop one response after Stripe has
  applied the refund (timeout after success, really applied at Stripe);
- "direct" scenarios call the adapter alone, to observe Stripe's own
  contract (key replay, changed parameters, concurrent same key, how
  soon a new refund is listed).

Ground truth is Stripe's record, read separately after each scenario
(the refunds listed for that PaymentIntent), never SafeAct's view.
Every HTTP call is metered (method, path, status, Stripe request id,
latency, replay header): never the key, never the Authorization header.
A run stops if the call budget in the manifest would be exceeded, or if
any object is live-mode. The output directory is secret-scanned.
"""

import argparse
import json
import platform
import sys
import threading
import time
import uuid

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import httpx2 as httpx
import psycopg
import sqlalchemy as sa

from psycopg import sql
from sqlalchemy.pool import NullPool

from connectors.base import DispatchKind, DispatchResult
from connectors.stripe_fake import FakeStripe
from connectors.stripe_key import (
    StripeKey,
    load_stripe_key,
    require_sandbox_allowed,
)
from connectors.stripe_payments import (
    API_BASE,
    LiveModeRefused,
    StripePaymentsConnector,
)
from core.connector_profiles import STRIPE, STRIPE_API_VERSION, stripe_profile
from core.provider_keys import derive_provider_key
from deploy.demo.bootstrap import Environment, rebuild, teardown_statements
from deploy.evaluation.agent_v1.secret_scan import scan
from storage.database import local_admin_settings, local_database_url
from storage.experimental_durable_execution import (
    ExecutionWorker,
    InMemoryTaskQueue,
    relay_once,
)
from storage.experimental_recovery_worker import Reconciler
from storage.state_projection import assert_no_drift
from storage.submission import IntentSubmission, submit_intent


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "deploy/sandbox/manifest_stripe_v1.json"
ACCOUNT = "stripe-sandbox"
TERMINAL = {"SUCCEEDED", "FAILED_CONFIRMED", "ESCALATED_UNRESOLVED"}
FAKE_KEY = "rk_test_" + "OfflineFakeOnly" * 2


# Sandbox-run timings: Stripe's 24 h key retention and 1 h margin are
# unchanged; only the investigation windows are shortened so a run ends.
RUN_PROFILE = stripe_profile(
    "v1-run", request_deadline=timedelta(seconds=30),
    investigation_window=timedelta(seconds=60),
    check_interval=timedelta(seconds=10),
    horizon=timedelta(seconds=600),
)


class BudgetExceeded(RuntimeError):
    pass


class MeteredTransport(httpx.BaseTransport):
    """Counts every call and keeps only non-secret metadata."""

    def __init__(self, inner, budget):
        self._inner = inner
        self._budget = budget
        self._lock = threading.Lock()
        self.calls = []

    def handle_request(self, request):
        with self._lock:
            if len(self.calls) >= self._budget:
                raise BudgetExceeded("call budget reached")
            key = request.headers.get("idempotency-key")
            entry = {
                "method": request.method, "path": request.url.path,
                # A tag, not the key: the 64-hex key is secret-shaped.
                "key_tag": key[4:20] if key else None,
                "at": datetime.now(timezone.utc).isoformat(),
            }
            self.calls.append(entry)
        started = time.monotonic()
        try:
            response = self._inner.handle_request(request)
        except httpx.HTTPError as error:
            entry["error"] = type(error).__name__
            raise
        finally:
            entry["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
        entry["status"] = response.status_code
        entry["request_id"] = response.headers.get("request-id")
        entry["replayed"] = response.headers.get("idempotent-replayed")
        return response


class LoseResponses:
    """Harness wrapper: drop the response of chosen keys once.

    The refund really happens at Stripe; SafeAct only sees no response.
    """

    def __init__(self, inner):
        self._inner = inner
        self.connector_id = inner.connector_id
        self.lose = set()
        self.dropped = {}

    def accepts(self, intent):
        return self._inner.accepts(intent)

    def dispatch(self, intent, *, operation_id, timeout=None):
        result = self._inner.dispatch(intent, operation_id=operation_id,
                                      timeout=timeout)
        key = intent.provider_idempotency_key
        if key in self.lose:
            self.lose.discard(key)
            self.dropped[key] = result.kind.value
            return DispatchResult(DispatchKind.AMBIGUOUS,
                                  "ambiguous_no_response")
        return result

    def lookup(self, target, profile):
        return self._inner.lookup(target, profile)


@dataclass
class Stripe:
    """Fixture and observer calls, through the same metered transport."""

    http: httpx.Client

    def payment_intent(self, amount):
        response = self.http.post("/v1/payment_intents", data={
            "amount": str(amount), "currency": "usd",
            "payment_method": "pm_card_visa", "confirm": "true",
            "automatic_payment_methods[enabled]": "true",
            "automatic_payment_methods[allow_redirects]": "never",
        })
        body = response.json()
        if body.get("livemode") is not False:
            raise LiveModeRefused("fixture PaymentIntent is not test mode")
        if response.status_code != 200 or not str(body.get("id", "")
                                                    ).startswith("pi_"):
            raise RuntimeError(f"fixture failed: HTTP {response.status_code}")
        return body["id"]

    def refunds(self, intent):
        response = self.http.get("/v1/refunds", params={
            "payment_intent": intent, "limit": "100"})
        if response.status_code != 200:
            raise RuntimeError(f"observer failed: HTTP {response.status_code}")
        data = response.json()["data"]
        if any(r.get("livemode") is True for r in data):
            raise LiveModeRefused("live-mode refund observed")
        return [{"id": r["id"], "amount": r["amount"], "status": r["status"],
                 "operation": (r.get("metadata") or {}).get(
                     "safeact_operation_id")} for r in data]


class Run:

    def __init__(self, *, key, transport, base_url, out, budget):
        self.out = out
        self.meter = MeteredTransport(transport, budget)
        self.connector = StripePaymentsConnector(
            key, transport=self.meter, base_url=base_url)
        self.wrapper = LoseResponses(self.connector)
        self.stripe = Stripe(httpx.Client(
            base_url=base_url, transport=self.meter, follow_redirects=False,
            headers={"Authorization": f"Bearer {key.reveal()}",
                     "Stripe-Version": STRIPE_API_VERSION},
        ))

    # -- environment -------------------------------------------------------

    def __enter__(self):
        self.admin = psycopg.connect(**local_admin_settings(), autocommit=True)
        self.env = Environment.disposable(uuid.uuid4().hex)
        self.passwords, _ = rebuild(self.admin, self.env,
                                    profiles=(RUN_PROFILE,), accounts=())
        self.engine = sa.create_engine(
            local_database_url(), poolclass=NullPool,
            connect_args={"options": f"-c search_path={self.env.schema},public"},
        )
        self.queue = InMemoryTaskQueue()
        self.worker = ExecutionWorker(
            connect=lambda: self.connect(self.env.worker),
            schema=self.env.schema, connectors=(self.wrapper,),
            lease_seconds=60,
        )
        self.reconciler = Reconciler(
            connect=lambda: self.connect(self.env.recoverer),
            schema=self.env.schema, connectors=(self.wrapper,),
        )
        return self

    def __exit__(self, *exc):
        try:
            assert_no_drift(self.admin, self.env.schema)
        finally:
            self.engine.dispose()
            self.admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE usename = ANY(%s)", (list(self.env.roles),))
            with self.admin.transaction():
                for statement in teardown_statements(self.admin, self.env):
                    self.admin.execute(statement)
            self.admin.close()

    def connect(self, role):
        settings = local_admin_settings()
        settings.update(user=role, password=self.passwords[role])
        return psycopg.connect(**settings)

    # -- SafeAct path --------------------------------------------------------

    def admit(self, intent, amount):
        receipt = submit_intent(self.engine, IntentSubmission(
            tenant_id="sandbox", actor_id="sandbox-runner",
            request_key="st-" + uuid.uuid4().hex, action_type="refund_payment",
            resource_id=intent, provider_account_id=ACCOUNT,
            connector_id=STRIPE, contract_version=RUN_PROFILE.version,
            payload={"payment_intent": intent, "amount_minor": amount},
        ))
        return receipt.operation_id

    def state(self, operation_id):
        row = self.admin.execute(sql.SQL(
            "SELECT c.state, o.provider_idempotency_key FROM {}.{} c "
            "JOIN {}.safeact_operations o USING (operation_id) "
            "WHERE operation_id = %s"
        ).format(sql.Identifier(self.env.schema),
                 sql.Identifier("safeact_operation_current_state"),
                 sql.Identifier(self.env.schema)), (operation_id,)).fetchone()
        return row

    def reasons(self, operation_id):
        return [r[0] for r in self.admin.execute(sql.SQL(
            "SELECT reason FROM {}.safeact_operation_transitions "
            "WHERE operation_id = %s ORDER BY seq"
        ).format(sql.Identifier(self.env.schema)), (operation_id,))]

    def evidence_statuses(self, operation_id):
        """Refund statuses SafeAct recorded as evidence detail, in order."""
        return [r[0] for r in self.admin.execute(sql.SQL(
            "SELECT details -> 'provider' ->> 'refund_status' "
            "FROM {}.safeact_attempt_outcomes WHERE operation_id = %s "
            "AND details ? 'provider' ORDER BY created_at"
        ).format(sql.Identifier(self.env.schema)), (operation_id,))]

    def drive(self, operation_id, *, passes=90, pause=1.0):
        for _ in range(passes):
            connection = self.connect(self.env.worker)
            try:
                relay_once(connection, self.env.schema, self.queue)
            finally:
                connection.close()
            self.worker.run_until_idle(self.queue)
            state, _ = self.state(operation_id)
            if state in TERMINAL:
                return state
            self.reconciler.reconcile_once()
            state, _ = self.state(operation_id)
            if state in TERMINAL:
                return state
            time.sleep(pause)
        return self.state(operation_id)[0]

    def through(self, amount, total, *, lose=False, intent=None):
        intent = intent or self.stripe.payment_intent(total)
        operation_id = self.admit(intent, amount)
        if lose:
            self.wrapper.lose.add(self.state(operation_id)[1])
        state = self.drive(operation_id)
        key = self.state(operation_id)[1]
        return intent, {
            "operation_id": str(operation_id), "state": state,
            "reasons": self.reasons(operation_id),
            "refund_posts": sum(1 for c in self.meter.calls
                                if c["key_tag"] == key[4:20]),
            "dropped_response_was": self.wrapper.dropped.get(key),
            "evidence_refund_status": (self.evidence_statuses(operation_id)
                                       or [None])[-1],
        }

    # -- direct contract observations ---------------------------------------

    def direct_intent(self, intent, amount, key):
        from types import SimpleNamespace
        from core.contracts import canonical_payload_hash

        payload = {"amount_minor": amount, "payment_intent": intent}
        return SimpleNamespace(
            connector_id=STRIPE, action_type="refund_payment",
            provider_idempotency_key=key,
            payload_canonical=json.dumps(payload, sort_keys=True,
                                         separators=(",", ":")),
            payload_sha256=canonical_payload_hash(payload),
        )

    def key(self):
        return derive_provider_key("sandbox", "direct-" + uuid.uuid4().hex,
                                   STRIPE, ACCOUNT)

    def send(self, intent, amount, key, operation_id):
        result = self.connector.dispatch(
            self.direct_intent(intent, amount, key),
            operation_id=operation_id, timeout=30)
        return {"kind": result.kind.value, "reason": result.reason,
                "reference": result.reference}

    # -- scenarios -----------------------------------------------------------

    def scenario(self, spec):
        kind = spec["kind"]
        amount, total = spec.get("amount", 500), spec.get("total", 2000)
        if kind == "through":
            intent, observed = self.through(amount, total,
                                            lose=spec.get("lose", False))
        elif kind == "through_twice":
            intent = self.stripe.payment_intent(total)
            _, first = self.through(total, total, intent=intent)
            _, second = self.through(total, total, intent=intent)
            observed = {"first": first, "second": second,
                        "state": second["state"]}
        elif kind == "replay":
            intent, key, op = self.stripe.payment_intent(total), self.key(), \
                uuid.uuid4()
            first = self.send(intent, amount, key, op)
            again = self.send(intent, amount, key, op)
            replayed = [c["replayed"] for c in self.meter.calls
                        if c["key_tag"] == key[4:20]]
            observed = {"first": first, "again": again,
                        "same_reference": first["reference"]
                        == again["reference"], "replay_headers": replayed}
        elif kind == "changed_params":
            intent, key, op = self.stripe.payment_intent(total), self.key(), \
                uuid.uuid4()
            observed = {"first": self.send(intent, amount, key, op),
                        "changed": self.send(intent, amount + 1, key, op)}
        elif kind == "concurrent":
            intent, key, op = self.stripe.payment_intent(total), self.key(), \
                uuid.uuid4()
            results = []
            start = threading.Barrier(2)

            def send():
                start.wait()
                results.append(self.send(intent, amount, key, op))
            threads = [threading.Thread(target=send) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            observed = {"results": sorted(r["reason"] for r in results),
                        "statuses": sorted(c.get("status") for c in
                                           self.meter.calls
                                           if c["key_tag"] == key[4:20])}
        elif kind == "visibility":
            intent, key, op = self.stripe.payment_intent(total), self.key(), \
                uuid.uuid4()
            sent = time.monotonic()
            self.send(intent, amount, key, op)
            seen_after = None
            for attempt in range(10):
                if self.stripe.refunds(intent):
                    seen_after = round((time.monotonic() - sent) * 1000)
                    break
                time.sleep(0.5)
            observed = {"listed_after_ms": seen_after,
                        "list_attempts": attempt + 1}
        else:
            raise ValueError(f"unknown scenario kind {kind}")
        refunds = self.stripe.refunds(intent)
        observed["stripe_refunds"] = len(refunds)
        observed["stripe_refund_ids"] = [r["id"] for r in refunds]
        observed["stripe_refund_statuses"] = [r["status"] for r in refunds]
        return observed


def check(spec, observed):
    """Compare against the frozen expectation; report every difference."""

    failures = []
    for field, expected in spec["expect"].items():
        actual = observed
        for part in field.split("."):
            actual = actual.get(part) if isinstance(actual, dict) else None
        if expected == "observed":
            continue
        if isinstance(expected, dict) and "one_of" in expected:
            if actual not in expected["one_of"]:
                failures.append({"field": field, "expected": expected,
                                 "actual": actual})
            continue
        if actual != expected:
            failures.append({"field": field, "expected": expected,
                             "actual": actual})
    return failures


def manifest_digest(manifest):
    return sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def run(args):
    if not args.fake:
        # Before anything else, so a refused run touches nothing.
        require_sandbox_allowed()
    manifest = json.loads(Path(args.manifest).read_text("utf-8"))
    if manifest["stripe_version"] != STRIPE_API_VERSION:
        raise SystemExit("Manifest pins another Stripe-Version")
    if manifest["profile"] != RUN_PROFILE.row():
        raise SystemExit("Manifest pins another connector contract")
    if manifest["limits"] != RUN_PROFILE.limits.row(
            RUN_PROFILE.connector_id, RUN_PROFILE.version):
        raise SystemExit("Manifest pins other F16 limits")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if args.fake:
        fake = FakeStripe(FAKE_KEY)
        key, transport, base_url = (StripeKey(FAKE_KEY), fake.transport(),
                                    "https://fake.stripe.test")
        mode = "fake (offline, documented-behaviour double)"
    else:
        key = load_stripe_key()
        transport = httpx.HTTPTransport(retries=0)
        base_url, mode = API_BASE, "stripe sandbox (test mode)"

    rows = []
    started = datetime.now(timezone.utc)
    with Run(key=key, transport=transport, base_url=base_url, out=out,
             budget=manifest["call_budget"]) as runner:
        for repeat in range(1, manifest["repeats"] + 1):
            for spec in manifest["scenarios"]:
                at = time.monotonic()
                try:
                    observed = runner.scenario(spec)
                    error = None
                except (BudgetExceeded, LiveModeRefused):
                    raise
                except Exception as failure:  # recorded, never hidden
                    observed, error = {}, type(failure).__name__
                failures = check(spec, observed) if error is None else []
                rows.append({
                    "scenario": spec["id"], "repeat": repeat,
                    "seconds": round(time.monotonic() - at, 2),
                    "observed": observed, "error": error,
                    "differences": failures,
                    "result": "ERROR" if error else
                              ("PASS" if not failures else "DIFFERENT"),
                })
                print(f"{spec['id']} r{repeat}: {rows[-1]['result']}",
                      flush=True)
        calls = list(runner.meter.calls)

    summary = {
        "mode": mode, "stripe_version": STRIPE_API_VERSION,
        "manifest_sha256": manifest_digest(manifest),
        "started": started.isoformat(),
        "finished": datetime.now(timezone.utc).isoformat(),
        "calls": len(calls), "call_budget": manifest["call_budget"],
        "results": {r: sum(1 for x in rows if x["result"] == r)
                    for r in ("PASS", "DIFFERENT", "ERROR")},
        "client": platform.platform(), "python": platform.python_version(),
        "cost_usd": 0.0 if args.fake else "0 (Stripe sandbox; no charges)",
    }
    (out / "results.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    (out / "calls.jsonl").write_text(
        "".join(json.dumps(c, sort_keys=True) + "\n" for c in calls))
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    findings = scan([out])
    if findings:
        raise SystemExit(f"Secret-shaped text in the output: {findings}")
    print(json.dumps(summary["results"]))
    return int(
        summary["results"]["DIFFERENT"] > 0
        or summary["results"]["ERROR"] > 0
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", default=str(MANIFEST))
    parser.add_argument("--out", required=True)
    parser.add_argument("--fake", action="store_true",
                        help="offline, against connectors/stripe_fake.py")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
