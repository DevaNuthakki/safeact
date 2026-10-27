"""Independent effects ledger, owned by the test or evaluation harness.

Mock providers only append to it. Nothing in connector, worker or
reconciler code may read it (enforced by a structural test), so
effects are counted from the provider side, never from what SafeAct
reports.
"""

from copy import deepcopy
from threading import Lock
from time import monotonic, monotonic_ns, time

from connectors.http_trace import correlation_hash, payload_hash


class EffectsLedger:

    def __init__(self):
        self._lock = Lock()
        self._calls = []
        self._effects = []
        self._call_times = []
        # Wall-clock times (seconds since the epoch), so a harness can order
        # provider calls and effects against database timestamps.
        self._timeline = []
        self._events = []

    def _event(self, event, **details):
        self._events.append(dict(details, event=event, source="provider",
                                 at=time(), monotonic_ns=monotonic_ns(),
                                 sequence=len(self._events)))

    # -- written by mock providers ---------------------------------------

    def record_call(self, correlation, params):
        with self._lock:
            self._calls.append((correlation, dict(params)))
            self._call_times.append((correlation, monotonic()))
            self._timeline.append(("call", correlation, time()))
            self._event("call", correlation_sha256=correlation_hash(correlation),
                        payload_sha256=payload_hash(params))

    def record_effect(self, correlation, effect_id):
        with self._lock:
            self._effects.append((correlation, effect_id))
            self._timeline.append(("effect", correlation, time()))
            self._event("effect", correlation_sha256=correlation_hash(correlation),
                        effect_id=effect_id)

    def record_observation(self, event, **details):
        if not event.startswith("provider_"):
            raise ValueError("Provider observations cannot create call/effect entries")
        with self._lock:
            self._event(event, **details)

    # -- read by the harness only ------------------------------------------

    def snapshot(self):
        with self._lock:
            return tuple(self._calls), tuple(self._effects)

    def call_times(self):
        with self._lock:
            return tuple(self._call_times)

    def timeline(self):
        """(event, correlation, wall time) for every call and effect."""
        with self._lock:
            return tuple(self._timeline)

    def events(self):
        """Independent provider observations with monotonic clocks and sequence."""
        with self._lock:
            return tuple(deepcopy(self._events))

    def calls_for(self, correlation):
        with self._lock:
            return tuple(p for c, p in self._calls if c == correlation)

    def effects_for(self, correlation):
        with self._lock:
            return tuple(e for c, e in self._effects if c == correlation)
