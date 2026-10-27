"""Provider controls and held commits used only by development qualification."""

import time
import uuid

from chaos.profiles import PAYMENTS_SHORT_RETENTION_SECONDS
from connectors.http_trace import correlation_hash
from connectors.payments_mock import LookupUnavailable, MockPaymentsProvider, ProviderLookup


class ObservedOutage:
    """Set and observe the outage switch under the provider's request lock."""

    def __init__(self, kind, provider, observer):
        self.kind, self.provider, self.observer = kind, provider, observer
        self.active_id = None

    def set(self, active):
        if type(active) is not bool:
            raise ValueError("Outage state must be boolean")
        with self.provider._lock:
            if active == self.provider.outage:
                raise RuntimeError("Outage control did not change provider state")
            if active:
                self.active_id = uuid.uuid4().hex
            elif self.active_id is None:
                raise RuntimeError("Outage has no matching owned start")
            self.provider.outage = active
            self.observer("provider_outage_changed", provider=self.kind,
                          outage_id=self.active_id, active=active)
            if not active:
                self.active_id = None


def release_after_escalation(held, cancelled, sample, emit, *, timeout=18):
    """Release a consumed request only after a committed DB escalation sample.

    sample uses an independent autocommit connection. Cancellation leaves the
    hold for the normal stop-execution / close-admission / release barrier.
    A scheduled delay or the arm's reported view cannot release this hold.
    """
    deadline = time.monotonic() + timeout
    while not cancelled.is_set() and time.monotonic() < deadline:
        if held.started.is_set():
            rows = sample()
            if len(rows) > 1:
                raise RuntimeError("Late-commit fixture requires one admitted operation")
            if rows and rows[0]["state"] == "ESCALATED_UNRESOLVED":
                row = rows[0]
                if row.get("escalated_at") is None or row.get("source") != "database":
                    raise RuntimeError("Committed escalation evidence is incomplete")
                held.release()
                emit("late_commit_released", operation_id=row["operation_id"],
                     escalation_sample_finished_ns=row["sample_finished_ns"])
                while not cancelled.is_set() and time.monotonic() < deadline:
                    if held.wait_committed(0.05):
                        return True
                break
        cancelled.wait(0.05)
    if cancelled.is_set():
        return False
    raise RuntimeError("Late-commit fixture did not observe escalation and commit in time")


class ShortRetentionPayments(MockPaymentsProvider):
    """Qualification payments: per-key retention and lookup visibility.

    q-v2-short keys expire after 6 s. q-v2-eventual keys use the delayed
    lookup index. Other keys keep the default strong lookup. Dispatch's
    native-key protections are independent of lookup visibility.
    """

    def __init__(self, *, ledger):
        super().__init__(ledger=ledger)
        self.short_keys = set()
        self.eventual_keys = set()

    def register_key(self, key, version):
        if not isinstance(key, str) or not key or version not in (
                "q-v2", "q-v2-eventual", "q-v2-short", "q-v2-inflight", "q-v2-eventual-inflight"):
            raise ValueError("Invalid qualification payment key or contract")
        with self._lock:
            if version == "q-v2-short":
                self.short_keys.add(key)
            if version in ("q-v2-eventual", "q-v2-eventual-inflight"):
                self.eventual_keys.add(key)

    def lookup(self, key):
        if key not in self.eventual_keys:
            return super().lookup(key)
        # The eventual lookup index cannot see an accepted, uncommitted
        # request. Native-key execution still uses the unchanged _in_flight
        # and _by_key guards, independently of what this index can see.
        with self._lock:
            if self.lookup_unavailable or self.outage:
                raise LookupUnavailable(key)
            self._expire()
            existing = self._by_key.get(key)
            if existing is None or key in self._hidden:
                return ProviderLookup("absent")
            return ProviderLookup("found", existing[0], dict(existing[1]))

    def _apply(self, key, params, prefix):
        receipt = super()._apply(key, params, prefix)
        if key in self.eventual_keys and self.hide_new_effects:
            self._hidden.add(key)
        self._ledger.record_observation("provider_key_stored", provider="payments",
            correlation_sha256=correlation_hash(key), effect_id=receipt.refund_id,
            retention_seconds=(PAYMENTS_SHORT_RETENTION_SECONDS if key in self.short_keys
                               else self.retention_seconds))
        return receipt

    def _expire(self):
        now = self._clock()
        expired = [(key, effect_id, now - seen,
                    PAYMENTS_SHORT_RETENTION_SECONDS if key in self.short_keys else self.retention_seconds)
                   for key, (effect_id, _, seen) in self._by_key.items()
                   if now - seen >= (PAYMENTS_SHORT_RETENTION_SECONDS
                                     if key in self.short_keys else self.retention_seconds)]
        for key, effect_id, age, retention in expired:
            del self._by_key[key]
            self._hidden.discard(key)
            self._ledger.record_observation("provider_key_expired", provider="payments",
                correlation_sha256=correlation_hash(key), effect_id=effect_id,
                age_seconds=age, retention_seconds=retention,
                key_present_before=True, key_present_after=key in self._by_key)


def release_after_empty_lookup(held, cancelled, observe, emit, *, provider,
                               resources, minimum_seconds=0, timeout=18):
    """Wait for a real empty HTTP lookup of the consumed request, then commit.

    The configured release delay is a minimum, measured after acceptance.
    Cancellation leaves release to the normal ordered cleanup barrier.
    """
    from chaos.lookup_trace import empty_lookup_windows

    if len(resources) != 1 or minimum_seconds < 0 or minimum_seconds >= timeout:
        raise ValueError("Empty-lookup hold requires one target and a bounded release delay")
    deadline = time.monotonic() + timeout
    accepted = None
    while not cancelled.is_set() and time.monotonic() < deadline:
        if held.started.is_set():
            accepted = time.monotonic() if accepted is None else accepted
            raw = observe()
            calls = [e for e in raw.get("ledger", []) if e.get("event") == "call"
                     and e.get("resource") in resources]
            keys = {e.get("correlation_sha256") for e in calls}
            if len(keys) > 1:
                raise RuntimeError("Held request attribution is ambiguous")
            for window in empty_lookup_windows(raw):
                mine = [e for e in calls if e.get("correlation_sha256") == window["correlation_sha256"]]
                if (window["provider"] != provider or not mine
                        or min(e["monotonic_ns"] for e in mine) >= window["lookup_started_ns"]
                        or any(e.get("event") == "effect" and e.get("correlation_sha256") in keys
                               for e in raw.get("ledger", []))
                        or time.monotonic() - accepted < minimum_seconds):
                    continue
                emit("empty_lookup_observed", **window)
                held.release()
                emit("hold_released", provider=provider)
                while not cancelled.is_set() and time.monotonic() < deadline:
                    if held.wait_committed(0.05):
                        return True
                break
        cancelled.wait(0.05)
    if cancelled.is_set():
        return False
    raise RuntimeError("Held request did not receive an empty HTTP lookup and commit in time")
