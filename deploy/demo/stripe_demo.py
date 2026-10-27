"""Optional Stripe test-mode demo profile (DEMO ONLY; Stripe test mode only).

The mock demo stays the default. This profile adds one console action,
"Refund a Stripe test payment" (refund_stripe_test_payment), which SafeAct
admits as ``refund_payment`` on connector ``stripe-test`` and executes
through the existing adapter (connectors/stripe_payments.py) and every one
of its gates:
- the restricted ``rk_test_`` key file (connectors/stripe_key.py), loaded
  by the process, never printed or logged;
- ``SAFEACT_ALLOW_STRIPE_SANDBOX=1``, and never with CI or GITHUB_ACTIONS set;
- any live-mode object raises LiveModeRefused and stops the demo stack.
Agents never reach it: the MCP tools offer only the mock actions.

    export SAFEACT_ALLOW_STRIPE_SANDBOX=1
    .venv/bin/python -m deploy.demo.stripe_demo setup    # 2 test PaymentIntents
    .venv/bin/python -m deploy.demo.bootstrap --stripe    # + contract and grants
    .venv/bin/python -m deploy.demo.run_stack --stripe    # API :8000 (console: npm run dev)
    ... as demo-north-requester, refund each PaymentIntent once ...
    .venv/bin/python -m deploy.demo.stripe_demo check    # exactly one refund each

The two PaymentIntents (recorded in .demo-stripe.json, mode 600, gitignored):
  normal          an ordinary refund: SUCCEEDED from Stripe's answer.
  response_lost   Stripe applies the refund, then this harness drops the
                  response before SafeAct sees it (timeout after success,
                  really applied at Stripe). SafeAct records UNKNOWN, the
                  reconciler finds the refund by its metadata, and the
                  operation is SUCCEEDED with provider evidence and no
                  second POST. Stripe's dashboard shows one refund.

Only the first response for that PaymentIntent is dropped; lookups and any
same-key request see Stripe's honest answers. Which response is dropped is
fixed by the fixture file, never chosen through the API.
"""

import argparse
import json
import os
import sys
import tempfile
import threading
import time

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx2 as httpx

from connectors.base import DispatchKind, DispatchResult
from connectors.stripe_key import load_stripe_key, require_sandbox_allowed
from connectors.stripe_payments import (
    API_BASE,
    LiveModeRefused,
    StripePaymentsConnector,
)
from core.connector_profiles import STRIPE, STRIPE_API_VERSION, stripe_profile


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / ".demo-stripe.json"
CALLS = ROOT / ".demo-stripe-calls.jsonl"
ACCOUNT = "stripe-test-account"
ACTION = "refund_payment"
CASES = ("normal", "response_lost")
CHARGE_MINOR = 2000
DASHBOARD = "https://dashboard.stripe.com/test/payments/"

# Stripe's documented 24 h key retention (less 1 h) is unchanged; only the
# investigation and escalation windows are shortened for a short walkthrough.
PROFILE = stripe_profile(
    "v1-demo", investigation_window=timedelta(seconds=90),
    check_interval=timedelta(seconds=15), horizon=timedelta(minutes=10),
)


def read_fixtures(path=FIXTURES):
    path = Path(path)
    if not path.is_file():
        raise RuntimeError("No Stripe demo fixtures: run "
                           "python -m deploy.demo.stripe_demo setup")
    if path.stat().st_mode & 0o077:
        raise RuntimeError("Unsafe Stripe demo fixture permissions")
    fixtures = json.loads(path.read_text("utf-8"))
    for case in CASES:
        intent = fixtures.get(case) if isinstance(fixtures, dict) else None
        if (type(intent) is not str or not intent.startswith("pi_")
                or not intent[3:].isalnum() or len(intent) > 67):
            raise RuntimeError("Invalid Stripe demo fixtures file")
    if fixtures["normal"] == fixtures["response_lost"]:
        raise RuntimeError("Invalid Stripe demo fixtures file")
    return fixtures


def grants(fixtures):
    """Exact grants: a refund of each fixture PaymentIntent, nothing else."""
    return tuple((STRIPE, ACCOUNT, ACTION, fixtures[case], PROFILE.version)
                 for case in CASES)


class DropFirstResponse:
    """Harness wrapper: lose the first dispatch response for one PaymentIntent.

    Stripe has already answered; SafeAct only sees no response. A live-mode
    object calls ``on_live`` (the stack stops) and is re-raised.
    """

    def __init__(self, inner, intent, *, on_live=None):
        self._inner = inner
        self.connector_id = inner.connector_id
        self._intent = intent
        self._on_live = on_live
        self._lock = threading.Lock()
        self.dropped = {}

    def accepts(self, intent):
        return self._inner.accepts(intent)

    def _guard(self, call):
        try:
            return call()
        except LiveModeRefused:
            if self._on_live is not None:
                self._on_live()
            raise

    def dispatch(self, intent, *, operation_id, timeout=None):
        result = self._guard(lambda: self._inner.dispatch(
            intent, operation_id=operation_id, timeout=timeout))
        try:
            target = json.loads(intent.payload_canonical).get("payment_intent")
        except (ValueError, AttributeError):
            target = None
        key = intent.provider_idempotency_key
        with self._lock:
            if target == self._intent and not self.dropped:
                self.dropped[key] = result.kind.value
                return DispatchResult(DispatchKind.AMBIGUOUS,
                                      "ambiguous_no_response")
        return result

    def lookup(self, target, profile):
        return self._guard(lambda: self._inner.lookup(target, profile))


class CallLog(httpx.BaseTransport):
    """Appends each Stripe call's non-secret metadata to a JSONL file.

    Method, path, status, Stripe's Request-Id, Idempotent-Replayed and a
    short tag of the derived idempotency key. Never the API key, never any
    header value besides those, never a body.
    """

    def __init__(self, inner, path):
        self._inner = inner
        self._path = Path(path)
        self._lock = threading.Lock()

    def handle_request(self, request):
        key = request.headers.get("idempotency-key")
        entry = {"at": datetime.now(timezone.utc).isoformat(),
                 "method": request.method, "path": request.url.path,
                 # A tag, not the key: the 64-hex key is secret-shaped.
                 "key_tag": key[4:20] if key else None}
        started = time.monotonic()
        try:
            response = self._inner.handle_request(request)
            entry.update(status=response.status_code,
                         request_id=response.headers.get("request-id"),
                         replayed=response.headers.get("idempotent-replayed"))
            return response
        except httpx.HTTPError as error:
            entry["error"] = type(error).__name__
            raise
        finally:
            entry["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
            with self._lock:
                fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, sort_keys=True) + "\n")


def _real_transport(base_url, call_log):
    """Only after the sandbox gate, only to api.stripe.com, always recorded."""
    require_sandbox_allowed()
    if base_url != API_BASE:
        raise ValueError("Real calls go to api.stripe.com only")
    return CallLog(httpx.HTTPTransport(retries=0), call_log)


def demo_connector(fixtures, *, key=None, transport=None, base_url=API_BASE,
                   on_live=None, call_log=CALLS):
    """The adapter, gated as always, inside the dropped-response wrapper."""
    if transport is None:
        transport = _real_transport(base_url, call_log)
    elif call_log is not None:
        transport = CallLog(transport, call_log)
    connector = StripePaymentsConnector(
        key if key is not None else load_stripe_key(),
        transport=transport, base_url=base_url)
    return DropFirstResponse(connector, fixtures["response_lost"], on_live=on_live)


def client(key, transport=None, base_url=API_BASE, call_log=CALLS):
    if transport is None:
        transport = _real_transport(base_url, call_log)
    elif call_log is not None:
        transport = CallLog(transport, call_log)
    return httpx.Client(
        base_url=base_url, transport=transport, follow_redirects=False,
        trust_env=False, timeout=30, headers={
            "Authorization": f"Bearer {key.reveal()}",
            "Stripe-Version": STRIPE_API_VERSION,
        })


def create_payment_intent(http, case):
    response = http.post("/v1/payment_intents", data={
        "amount": str(CHARGE_MINOR), "currency": "usd",
        "payment_method": "pm_card_visa", "confirm": "true",
        "automatic_payment_methods[enabled]": "true",
        "automatic_payment_methods[allow_redirects]": "never",
        "description": f"SafeAct demo ({case})",
    })
    body = response.json()
    if not isinstance(body, dict) or body.get("livemode") is not False:
        raise LiveModeRefused("fixture PaymentIntent is not test mode")
    if response.status_code != 200 or not str(body.get("id", "")).startswith("pi_"):
        raise RuntimeError(f"fixture failed: HTTP {response.status_code}")
    return body["id"]


def refunds(http, intent):
    response = http.get("/v1/refunds", params={"payment_intent": intent,
                                               "limit": "100"})
    if response.status_code != 200:
        raise RuntimeError(f"refund list failed: HTTP {response.status_code}")
    data = response.json()["data"]
    # The adapter's and the sandbox runner's rule (connectors/stripe_payments).
    if any(refund.get("livemode") is True for refund in data):
        raise LiveModeRefused("live-mode refund observed")
    return [{"id": r["id"], "amount": r["amount"], "status": r["status"],
             "operation": (r.get("metadata") or {}).get("safeact_operation_id")}
            for r in data]


def write_fixtures(path, fixtures):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".demo-stripe-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(fixtures, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def setup(path=FIXTURES, *, key=None, transport=None, base_url=API_BASE,
          call_log=CALLS):
    with client(key or load_stripe_key(), transport, base_url, call_log) as http:
        fixtures = {case: create_payment_intent(http, case) for case in CASES}
    fixtures["created"] = datetime.now(timezone.utc).isoformat()
    fixtures["amount_minor"] = CHARGE_MINOR
    write_fixtures(path, fixtures)
    return fixtures


def check(path=FIXTURES, *, key=None, transport=None, base_url=API_BASE,
          call_log=CALLS):
    """Stripe's own record: the refunds of each fixture PaymentIntent."""
    fixtures = read_fixtures(path)
    with client(key or load_stripe_key(), transport, base_url, call_log) as http:
        report = {case: refunds(http, fixtures[case]) for case in CASES}
    return report, all(len(report[case]) == 1 for case in CASES)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("setup", "check"))
    parser.add_argument("--fixtures", type=Path, default=FIXTURES)
    args = parser.parse_args(argv)
    # First, so a refused run reads no key and sends nothing.
    require_sandbox_allowed()
    if args.command == "setup":
        fixtures = setup(args.fixtures)
        for case in CASES:
            print(f"{case:14} {fixtures[case]}  {DASHBOARD}{fixtures[case]}")
        print(f"Fixtures in {args.fixtures} (test mode). Next: "
              "python -m deploy.demo.bootstrap --stripe")
        return 0
    report, ok = check(args.fixtures)
    for case, found in report.items():
        print(f"{case:14} {len(found)} refund(s) at Stripe: " + ", ".join(
            f"{r['id']} {r['amount']} {r['status']}" for r in found))
    print("PASS: exactly one refund per PaymentIntent" if ok else
          "DIFFERENT: expected exactly one refund per PaymentIntent")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
