"""A fake Stripe API v1 for offline tests (a test double, never Stripe).

An httpx transport that models, for refunds and the PaymentIntent
fixture, only the behaviour Stripe documents and the adapter relies on
(docs.stripe.com/api/idempotent_requests, /error-low-level,
/rate-limits, /api/refunds; read 2026-10-20):

- Bearer authentication with one restricted test key; anything else 401;
- an idempotency key saves the first response once execution begins
  (including a 500), and a replay returns it with Idempotent-Replayed;
- the same key with other parameters is a 400 idempotency_error;
- a second request while the first executes gets 409, nothing saved;
- validation failures, 401 and 429 (rate limit, with
  Stripe-Rate-Limited-Reason) are not saved: nothing executed;
- a refund larger than what is left is 400 amount_too_large, a fully
  refunded intent 400 charge_already_refunded (both saved);
- listing refunds is newest first, filtered by created[gte], paginated
  by starting_after, and new refunds can stay hidden (visibility delay).

Every applied refund is appended to an EffectsLedger the test owns; the
fake's answers come only from its own state. Faults are one-shot.
"""

import threading
import time
import uuid

from urllib.parse import parse_qsl

import httpx2 as httpx

from connectors.ledger import EffectsLedger


class FakeStripe:

    def __init__(self, key, *, ledger=None, clock=time.time):
        self._key = key
        self._clock = clock
        self.ledger = ledger or EffectsLedger()
        self._lock = threading.Lock()
        self._intents = {}       # pi -> {"amount", "refunded"}
        self._refunds = []       # newest last
        self._saved = {}         # idempotency key -> (form, status, body)
        self._executing = set()
        self._hidden = set()

        # One-shot faults.
        self.rate_limit_next = False
        self.lock_timeout_next = False
        self.server_error_after_effect = False
        self.drop_response_after_effect = False
        self.hide_new_refunds = False
        self.live_mode = False
        self.refund_status = "succeeded"   # what new refunds report
        self.hold = None         # threading.Event: execution waits on it

    def transport(self):
        return httpx.MockTransport(self._handle)

    # -- fixture helpers -------------------------------------------------

    def payment_intent(self, amount):
        intent = "pi_" + uuid.uuid4().hex[:24]
        with self._lock:
            self._intents[intent] = {"amount": amount, "refunded": 0}
        return intent

    def refunds_for(self, intent):
        with self._lock:
            return [dict(r) for r in self._refunds
                    if r["payment_intent"] == intent]

    def reveal(self):
        with self._lock:
            self._hidden.clear()

    # -- HTTP --------------------------------------------------------------

    def _handle(self, request):
        if request.headers.get("authorization") != f"Bearer {self._key}":
            return _error(401, "invalid_request_error", "api_key_invalid")
        if not request.headers.get("stripe-version"):
            return _error(400, "invalid_request_error", "version_missing")
        if request.method == "POST" and request.url.path == "/v1/refunds":
            return self._create(request)
        if request.method == "GET" and request.url.path == "/v1/refunds":
            return self._list(request)
        if request.method == "POST" and request.url.path == (
            "/v1/payment_intents"
        ):
            form = dict(parse_qsl(request.content.decode()))
            intent = self.payment_intent(int(form.get("amount", "0")))
            return httpx.Response(200, json={
                "id": intent, "object": "payment_intent",
                "livemode": self.live_mode, "status": "succeeded",
                "amount": int(form.get("amount", "0")),
            })
        return _error(404, "invalid_request_error", "resource_missing")

    def _create(self, request):
        form = dict(parse_qsl(request.content.decode()))
        key = request.headers.get("idempotency-key")

        with self._lock:
            if self.rate_limit_next:
                self.rate_limit_next = False
                response = _error(429, "invalid_request_error", "rate_limit")
                response.headers["Stripe-Rate-Limited-Reason"] = "global-rate"
                return response
            if self.lock_timeout_next:
                self.lock_timeout_next = False
                return _error(429, "invalid_request_error", "lock_timeout")
            if not form.get("payment_intent"):
                return _error(400, "invalid_request_error",
                              "parameter_missing")
            if key in self._executing:
                return _error(409, "idempotency_error",
                              "idempotency_key_in_use")
            saved = self._saved.get(key) if key else None
            if saved is not None:
                original, status, body = saved
                if original != form:
                    return _error(400, "idempotency_error",
                                  "idempotency_key_reused")
                return httpx.Response(status, json=body,
                                      headers={"Idempotent-Replayed": "true"})
            if key:
                self._executing.add(key)
            hold = self.hold

        try:
            if hold is not None:
                hold.wait(10)
            with self._lock:
                status, body, drop = self._execute(form)
                if key:
                    self._saved[key] = (form, status, body)
        finally:
            with self._lock:
                self._executing.discard(key)
        if drop:
            raise httpx.ReadTimeout("response lost after the refund")
        return httpx.Response(status, json=body,
                              headers={"Request-Id": "req_" + uuid.uuid4().hex})

    def _execute(self, form):
        intent = self._intents.get(form["payment_intent"])
        if intent is None:
            return (*_body(400, "invalid_request_error", "resource_missing"),
                    False)
        amount = int(form.get("amount") or intent["amount"] - intent["refunded"])
        if intent["refunded"] >= intent["amount"]:
            return (*_body(400, "invalid_request_error",
                           "charge_already_refunded"), False)
        if amount > intent["amount"] - intent["refunded"]:
            return (*_body(400, "invalid_request_error", "amount_too_large"),
                    False)

        refund = {
            "id": "re_" + uuid.uuid4().hex[:24], "object": "refund",
            "amount": amount, "payment_intent": form["payment_intent"],
            "status": self.refund_status, "created": int(self._clock()),
            "metadata": {
                k[len("metadata["):-1]: v for k, v in form.items()
                if k.startswith("metadata[")
            },
        }
        if self.live_mode:
            refund["livemode"] = True
        intent["refunded"] += amount
        self._refunds.append(refund)
        self.ledger.record_effect(form["payment_intent"], refund["id"])
        if self.hide_new_refunds:
            self.hide_new_refunds = False
            self._hidden.add(refund["id"])

        drop, self.drop_response_after_effect = (
            self.drop_response_after_effect, False)
        if self.server_error_after_effect:
            self.server_error_after_effect = False
            return (*_body(500, "api_error", None), drop)
        return 200, refund, drop

    def _list(self, request):
        params = dict(request.url.params)
        since = int(params.get("created[gte]", "0"))
        limit = int(params.get("limit", "10"))
        intent = params.get("payment_intent")
        with self._lock:
            visible = [r for r in reversed(self._refunds)
                       if r["created"] >= since and r["id"] not in self._hidden
                       and (intent is None or r["payment_intent"] == intent)]
        after = params.get("starting_after")
        if after:
            ids = [r["id"] for r in visible]
            visible = visible[ids.index(after) + 1:] if after in ids else []
        page = visible[:limit]
        return httpx.Response(200, json={
            "object": "list", "data": page,
            "has_more": len(visible) > limit,
        })


def _body(status, kind, code):
    error = {"type": kind}
    if code:
        error["code"] = code
    return status, {"error": error}


def _error(status, kind, code):
    status, body = _body(status, kind, code)
    return httpx.Response(status, json=body)


def form_of(request):
    """Decoded form body of a captured request (tests)."""
    return dict(parse_qsl(request.content.decode()))


__all__ = ["FakeStripe", "form_of"]
