"""Observe provider calls without reading their independent effects ledger."""

import uuid

from connectors.http_trace import correlation_hash, payload_hash


METHODS = frozenset({"refund", "charge", "create_record", "send", "lookup", "search"})


class ObservedProvider:
    def __init__(self, kind, provider, observer, *, barrier=None):
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_provider", provider)
        object.__setattr__(self, "_observer", observer)
        object.__setattr__(self, "_barrier", barrier)

    def __setattr__(self, name, value):
        setattr(self._provider, name, value)

    def __getattr__(self, name):
        method = getattr(self._provider, name)
        if name not in METHODS:
            return method

        def invoke(correlation, *args, **kwargs):
            details = dict(provider=self._kind, method=name,
                           provider_call_id=uuid.uuid4().hex,
                           correlation_sha256=correlation_hash(correlation))
            self._observer("provider_start", **details)
            try:
                result = method(correlation, *args, **kwargs)
            except Exception as error:
                self._observer("provider_error", **details,
                               error_type=type(error).__name__)
                raise
            summary = {}
            if name == "lookup":
                summary["lookup_status"] = result.status
                records = [(result.refund_id, result.params)] if result.status == "found" else []
            elif name == "search":
                summary["lookup_status"] = "found" if result else "absent"
                records = [(record.record_id, record.params) for record in result]
            else:
                records = None
            if records is not None:
                summary["lookup_records"] = [dict(effect_id=effect_id,
                    payload_sha256=payload_hash(params)) for effect_id, params in records]
            self._observer("provider_end", **details, **summary)
            return result
        if self._barrier is None:
            return invoke

        def admitted(*args, **kwargs):
            with self._barrier.request():
                return invoke(*args, **kwargs)
        return admitted
