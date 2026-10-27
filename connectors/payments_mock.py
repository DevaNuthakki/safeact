"""In-process payments provider mock (a test double, never a provider).

No network, no money, no credentials. Every call and every applied
effect is appended to the harness's ``EffectsLedger``; the mock's own
answers come only from its provider-side state, never from the ledger.

Provider behaviour modelled (payments profile):
- native idempotency: the same key with the same parameters returns
  the original refund without a second effect; the same key with
  different parameters is rejected; a request for a key whose first
  request is still in flight is refused (409-style) and applies nothing;
- bounded key retention: after ``retention_seconds`` on the mock's
  clock the key is forgotten, so a replay applies a second effect (the
  hazard F09 must block) and a lookup by that key sees nothing;
- lookup by key returns a raw answer: ``found`` (with the stored
  parameters), ``absent`` or ``in_progress``. STRONG semantics: every
  committed effect is visible. EVENTUAL: new effects may stay hidden.
  Turning an answer into evidence is the adapter's job, under the
  connector contract;
- a definitive rejection carries a reference and applies nothing;
- a 429-style rate limit is answered before execution: nothing applied,
  with a Retry-After (documented by this mock contract, as Stripe does);
- an outage: no response and nothing applied, lookups unavailable.
"""

from dataclasses import dataclass
from threading import Lock
from time import monotonic
from uuid import uuid4

from connectors.mock_support import HeldCall, await_held
from core.contracts import LookupSemantics


class ProviderTimeout(Exception):
    """No response. The effect may or may not have been applied."""


class ProviderServerError(Exception):
    """A 5xx-style response. It says nothing about the effect."""


class ProviderRejected(Exception):
    """Definitive rejection before execution: nothing was applied."""

    def __init__(self, reference, *, retryable=False):
        super().__init__(f"refund rejected: {reference}")
        self.reference = reference
        self.retryable = retryable


class IdempotencyConflict(Exception):
    """Same key, different parameters. Nothing new was applied."""


class ProviderInFlight(Exception):
    """Same key while the first request runs. Nothing new was applied."""


class LookupUnavailable(Exception):
    """The provider could not answer a lookup."""


class ProviderRateLimited(Exception):
    """429 before execution: nothing was applied; retry after a delay."""

    def __init__(self, retry_after=None):
        super().__init__("rate limited")
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class RefundReceipt:
    refund_id: str
    idempotency_key: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class ProviderLookup:
    status: str  # "found" | "absent" | "in_progress"
    refund_id: str | None = None
    params: dict | None = None


class MockPaymentsProvider:

    def __init__(
        self, *, ledger, lookup_semantics=LookupSemantics.STRONG,
        retention_seconds=24 * 3600, clock=monotonic,
    ):
        if lookup_semantics is LookupSemantics.UNAVAILABLE:
            raise ValueError("Payments mock requires a lookup")

        self.lookup_semantics = lookup_semantics
        self.retention_seconds = retention_seconds
        self._ledger = ledger
        self._clock = clock
        self._lock = Lock()
        self._by_key = {}    # key -> (refund_id, params, first_seen)
        self._in_flight = {}  # key -> params
        self._hidden = set()
        self._held = []

        # Fault switches. One-shot unless stated otherwise.
        self.fail_before_effect = False
        self.fail_after_effect = False
        self.server_error_after_effect = False
        self.reject_next = None
        self.reject_next_retryable = None
        self.lookup_unavailable = False
        self.hide_new_effects = False
        self.before_call = None
        # F16: the next N requests are rate limited (with retry_after
        # seconds); outage: every request and lookup fails until cleared.
        self.rate_limit_next = 0
        self.retry_after = None
        self.outage = False

    # -- provider surface ----------------------------------------------

    def refund(self, idempotency_key, params, *, timeout=None):
        return self._request("re_", idempotency_key, params, timeout)

    def charge(self, idempotency_key, params, *, timeout=None):
        return self._request("ch_", idempotency_key, params, timeout)

    def _request(self, prefix, idempotency_key, params, timeout):
        if type(idempotency_key) is not str or not idempotency_key:
            raise ValueError("Idempotency key required")
        if type(params) is not dict:
            raise TypeError("Request parameters must be a dict")

        if self.before_call is not None:
            self.before_call(idempotency_key, params)

        held = None
        with self._lock:
            self._ledger.record_call(idempotency_key, params)
            self._expire()

            if self.outage:
                raise ProviderTimeout("provider down, nothing applied")

            if self.rate_limit_next:
                self.rate_limit_next -= 1
                raise ProviderRateLimited(self.retry_after)

            if self.fail_before_effect:
                self.fail_before_effect = False
                raise ProviderTimeout("no response, nothing applied")

            if self.reject_next is not None:
                reference, self.reject_next = self.reject_next, None
                raise ProviderRejected(reference)

            if self.reject_next_retryable is not None:
                reference = self.reject_next_retryable
                self.reject_next_retryable = None
                # e.g. a documented "not processed, try again" answer.
                raise ProviderRejected(reference, retryable=True)

            if idempotency_key in self._in_flight:
                raise ProviderInFlight(idempotency_key)

            existing = self._by_key.get(idempotency_key)
            if existing is not None:
                refund_id, original, _ = existing
                if original != params:
                    raise IdempotencyConflict(idempotency_key)
                return RefundReceipt(refund_id, idempotency_key, True)

            if self._held:
                held = self._held.pop(0)
                self._in_flight[idempotency_key] = dict(params)
            else:
                receipt = self._apply(idempotency_key, params, prefix)

        if held is None:
            return self._respond(receipt)

        # Accepted but not committed: the caller waits, and may stop
        # waiting before the provider commits.
        def commit():
            with self._lock:
                return self._commit_held(idempotency_key, params, prefix)

        receipt = await_held(
            held, timeout, commit, ProviderTimeout("no answer yet")
        )
        return self._respond(receipt)

    def lookup(self, idempotency_key):
        with self._lock:
            if self.lookup_unavailable or self.outage:
                raise LookupUnavailable(idempotency_key)
            self._expire()

            if idempotency_key in self._in_flight:
                return ProviderLookup("in_progress")

            existing = self._by_key.get(idempotency_key)
            if existing is None or idempotency_key in self._hidden:
                return ProviderLookup("absent")
            return ProviderLookup("found", existing[0], dict(existing[1]))

    # -- test controls -------------------------------------------------

    def hold_next(self, *, respond_now=False):
        """The next new request is accepted but commits on release()."""

        held = HeldCall(respond_now=respond_now)
        with self._lock:
            self._held.append(held)
        return held

    def reveal_hidden_effects(self):
        with self._lock:
            self._hidden.clear()

    # -- internals -----------------------------------------------------

    def _respond(self, receipt):
        if self.fail_after_effect:
            self.fail_after_effect = False
            raise ProviderTimeout("response lost after effect")
        if self.server_error_after_effect:
            self.server_error_after_effect = False
            raise ProviderServerError("502 after commit")
        return receipt

    def _apply(self, key, params, prefix):
        refund_id = prefix + uuid4().hex
        self._by_key[key] = (refund_id, dict(params), self._clock())
        self._ledger.record_effect(key, refund_id)
        if (
            self.hide_new_effects
            and self.lookup_semantics is LookupSemantics.EVENTUAL
        ):
            self._hidden.add(key)
        return RefundReceipt(refund_id, key, False)

    def _commit_held(self, key, params, prefix):
        self._in_flight.pop(key, None)
        return self._apply(key, params, prefix)

    def _expire(self):
        cutoff = self._clock() - self.retention_seconds
        for key in [
            k for k, (_, _, seen) in self._by_key.items() if seen <= cutoff
        ]:
            del self._by_key[key]
            self._hidden.discard(key)

