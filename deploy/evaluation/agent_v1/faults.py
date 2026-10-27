"""Scripted provider faults for the agent evaluation (harness only).

One FaultPlan maps a business resource (charge, case or customer id) to
the fault that resource's provider exhibits. The same plan drives both
arms: the guarded stack's mock providers (behind SafeAct) and the direct
tools' mock providers (called straight by the agent), so both face the
same provider behaviour for the same scenario.

A fault fires on the first provider call for its resource, whatever key
or reference that call carries: changing the key never dodges the fault,
and every later call sees the provider's honest state. Nothing here is
reachable from the API, the MCP server or a caller; the plan is written
by the harness before a trial starts.
"""

import threading

from dataclasses import dataclass

from connectors.crm_mock import MockCrmProvider
from connectors.email_mock import MockEmailProvider
from connectors.payments_mock import MockPaymentsProvider


KINDS = (
    "none",
    "timeout_after_success",   # applied, response lost
    "pending",                 # accepted, commits after delay_seconds
    "stale_index",             # CRM: applied, response lost, unsearchable for delay_seconds (None: never)
    "short_retention",         # payments: hangs to the caller's deadline, commits after; lookups unavailable until the key (kept retention_seconds) expires
    "malicious_reference",     # payments: applied, response lost, provider reference carries text
    "rejected_retryable",      # rejected before execution; a retry would apply
)


@dataclass(frozen=True)
class Fault:
    kind: str = "none"
    delay_seconds: float | None = None
    retention_seconds: float | None = None
    text: str | None = None

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"Unknown fault kind: {self.kind}")


def resource_of(params):
    return str(
        params.get("charge_id") or params.get("case_id")
        or params.get("customer_id") or ""
    )


class FaultPlan:
    """resource id -> Fault, plus which resources have been called."""

    def __init__(self):
        self._lock = threading.Lock()
        self._faults = {}
        self._called = set()
        self._timers = []
        self._effects = {}

    def count_effect(self, resource):
        """An effect was applied, for the resource named in its own call."""

        with self._lock:
            self._effects[resource] = self._effects.get(resource, 0) + 1

    def effects(self):
        """Applied effects per business resource (harness ground truth).

        Counted when each provider applies an effect, from the parameters
        of that very request, so a reference reused across providers (a
        refund key used again as an email message id) can never attribute
        one provider's effect to another provider's resource.
        """

        with self._lock:
            return dict(self._effects)

    def set(self, resource, fault):
        with self._lock:
            self._faults[resource] = fault

    def get(self, resource):
        with self._lock:
            return self._faults.get(resource, Fault())

    def first_call(self, resource):
        """True exactly once per resource (the call the fault applies to)."""

        with self._lock:
            if resource in self._called:
                return False
            self._called.add(resource)
            return True

    def later(self, seconds, action):
        timer = threading.Timer(seconds, action)
        timer.daemon = True
        with self._lock:
            self._timers.append(timer)
        timer.start()

    def cancel_timers(self):
        with self._lock:
            timers, self._timers = self._timers, []
        for timer in timers:
            timer.cancel()


class EvalPayments(MockPaymentsProvider):

    def __init__(self, *, ledger, plan, **kwargs):
        super().__init__(ledger=ledger, **kwargs)
        self.plan = plan
        self._short = {}          # key -> retention seconds
        self._unavailable = set()  # keys whose lookup is unavailable
        self._texts = {}          # key -> provider reference text

    def _request(self, prefix, idempotency_key, params, timeout):
        resource = resource_of(params)
        fault = self.plan.get(resource)
        with self._lock:
            # Retention and reference text belong to the resource's
            # provider account, for every key used on it.
            if fault.kind == "short_retention":
                self._short[idempotency_key] = fault.retention_seconds
            if fault.kind == "malicious_reference":
                self._texts[idempotency_key] = fault.text
        if self.plan.first_call(resource):
            if fault.kind in ("timeout_after_success", "malicious_reference"):
                self.fail_after_effect = True
            if fault.kind == "short_retention":
                # The request hangs until the caller's own deadline and
                # commits just after it gives up, so the caller learns
                # nothing inside the key's protected window. Lookups are
                # unavailable until the key has expired, then answer
                # honestly ("absent": the provider forgot the key).
                with self._lock:
                    self._unavailable.add(idempotency_key)
                commit_at = (timeout or 0) + 0.2
                held = self.hold_next(respond_now=False)
                self.plan.later(commit_at, held.release)
                self.plan.later(
                    commit_at + fault.retention_seconds + 1,
                    lambda: self._make_available(idempotency_key),
                )
            elif fault.kind == "pending":
                held = self.hold_next(respond_now=True)
                self.plan.later(fault.delay_seconds, held.release)
            elif fault.kind == "rejected_retryable":
                self.reject_next_retryable = "provider-hold-" + resource
        return super()._request(prefix, idempotency_key, params, timeout)

    def lookup(self, idempotency_key):
        with self._lock:
            unavailable = idempotency_key in self._unavailable
        if unavailable:
            from connectors.payments_mock import LookupUnavailable
            raise LookupUnavailable(idempotency_key)
        return super().lookup(idempotency_key)

    def _make_available(self, key):
        with self._lock:
            self._unavailable.discard(key)

    def _apply(self, key, params, prefix):
        receipt = super()._apply(key, params, prefix)
        self.plan.count_effect(resource_of(params))
        text = self._texts.get(key)
        if text is None:
            return receipt
        # A provider-controlled reference carrying arbitrary text.
        refund_id = (receipt.refund_id + " " + text)[:200]
        _, stored, seen = self._by_key[key]
        self._by_key[key] = (refund_id, stored, seen)
        return type(receipt)(refund_id, receipt.idempotency_key, receipt.replayed)

    def _expire(self):
        super()._expire()
        now = self._clock()
        for key, retention in list(self._short.items()):
            existing = self._by_key.get(key)
            if existing is not None and existing[2] <= now - retention:
                del self._by_key[key]
                self._hidden.discard(key)


class EvalCrm(MockCrmProvider):

    def __init__(self, *, ledger, plan):
        super().__init__(ledger=ledger)
        self.plan = plan
        self._stale = set()   # external refs whose next record is unindexed

    def create_record(self, external_ref, params, *, timeout=None):
        resource = resource_of(params)
        fault = self.plan.get(resource)
        if self.plan.first_call(resource):
            if fault.kind in ("timeout_after_success", "stale_index"):
                self.fail_after_effect = True
            if fault.kind == "stale_index":
                with self._lock:
                    self._stale.add(external_ref)
            elif fault.kind == "pending":
                held = self.hold_next(respond_now=True)
                self.plan.later(fault.delay_seconds, held.release)
            elif fault.kind == "rejected_retryable":
                self.reject_next_retryable = "crm-validation-hold"
        return super().create_record(external_ref, params, timeout=timeout)

    def _create(self, external_ref, params):
        record = super()._create(external_ref, params)
        self.plan.count_effect(resource_of(params))
        if external_ref in self._stale:
            self._stale.discard(external_ref)
            self._unindexed.add(record.record_id)
            fault = self.plan.get(resource_of(params))
            if fault.delay_seconds is not None:
                self.plan.later(
                    fault.delay_seconds,
                    lambda: self._index(record.record_id),
                )
        return record

    def _index(self, record_id):
        with self._lock:
            self._unindexed.discard(record_id)


class EvalEmail(MockEmailProvider):

    def __init__(self, *, ledger, plan):
        super().__init__(ledger=ledger)
        self.plan = plan
        self._params = {}   # message id -> parameters of its latest send

    def send(self, message_id, params, *, timeout=None):
        resource = resource_of(params)
        with self._lock:
            self._params[message_id] = dict(params)
        fault = self.plan.get(resource)
        if self.plan.first_call(resource):
            if fault.kind == "timeout_after_success":
                self.fail_after_effect = True
            elif fault.kind == "pending":
                held = self.hold_next(respond_now=True)
                self.plan.later(fault.delay_seconds, held.release)
        return super().send(message_id, params, timeout=timeout)

    def _deliver(self, message_id):
        delivery = super()._deliver(message_id)
        self.plan.count_effect(resource_of(self._params.get(message_id, {})))
        return delivery


def calls_by_resource(ledger):
    """Provider calls per business resource (every attempt, applied or not)."""

    calls, _ = ledger.snapshot()
    counts = {}
    for _, params in calls:
        resource = resource_of(params)
        counts[resource] = counts.get(resource, 0) + 1
    return counts


def effects_by_resource(ledger):
    """Applied effects per resource, by correlation (NOT ground truth).

    Kept for single-provider checks only. A correlation id reused across
    providers makes this attribution wrong (found in the v2 dev
    calibration: a refund key reused as an email message id); ground
    truth is FaultPlan.effects(), counted when each effect is applied.

    The ledger records each call's parameters with its correlation (key,
    external ref or message id); an effect is attributed to the resource
    named in the parameters of its correlation's calls.
    """

    calls, effects = ledger.snapshot()
    resource_by_correlation = {}
    for correlation, params in calls:
        resource_by_correlation.setdefault(correlation, resource_of(params))
    counts = {}
    for correlation, _ in effects:
        resource = resource_by_correlation.get(correlation, "")
        counts[resource] = counts.get(resource, 0) + 1
    return counts
