"""Harness-only admin API for the HTTP mock providers.

Served on its own loopback port, never on a provider port, and never
called by connector, worker or reconciler code. It is how a test or
evaluation harness running in another process:
- reads the independent effects ledger (ground truth for effects);
- sets the mocks' one-shot fault switches;
- holds a request so it commits late, then releases it.

Fault switches are an allow-list per provider; anything else is refused.
"""

import threading
import uuid

from fastapi import FastAPI, HTTPException


FAULTS = {
    "payments": frozenset({
        "fail_before_effect", "fail_after_effect",
        "server_error_after_effect", "reject_next", "reject_next_retryable",
        "lookup_unavailable", "hide_new_effects",
        "rate_limit_next", "retry_after", "outage",
    }),
    "crm": frozenset({
        "fail_before_effect", "fail_after_effect",
        "server_error_after_effect", "reject_next", "reject_next_retryable",
        "search_unavailable", "index_lag", "drop_from_index",
        "corrupt_search", "rate_limit_next", "retry_after", "outage",
    }),
    "email": frozenset({
        "fail_before_effect", "fail_after_effect",
        "server_error_after_effect", "reject_next",
        "rate_limit_next", "retry_after", "outage",
    }),
}


def admin_app(ledger, providers):
    """providers: {"payments": mock, "crm": mock, "email": mock}."""

    app = FastAPI(title="mock-provider-admin", openapi_url=None,
                  docs_url=None, redoc_url=None)
    holds = {}
    lock = threading.Lock()

    def provider(kind):
        if kind not in providers:
            raise HTTPException(404, "unknown provider")
        return providers[kind]

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/ledger")
    def snapshot():
        calls, effects = ledger.snapshot()
        return {
            "calls": [[c, p] for c, p in calls],
            "effects": [[c, e] for c, e in effects],
            "call_times": [[c, t] for c, t in ledger.call_times()],
        }

    @app.get("/ledger/effects")
    def effects_for(correlation: str):
        return {"effects": list(ledger.effects_for(correlation))}

    @app.get("/ledger/calls")
    def calls_for(correlation: str):
        return {"calls": list(ledger.calls_for(correlation))}

    @app.post("/faults/{kind}")
    def set_faults(kind: str, switches: dict):
        mock = provider(kind)
        unknown = set(switches) - FAULTS[kind]
        if unknown:
            raise HTTPException(422, f"unknown fault switch: {sorted(unknown)}")
        for name, value in switches.items():
            setattr(mock, name, value)
        return {"set": sorted(switches)}

    @app.post("/holds/{kind}")
    def hold(kind: str, respond_now: bool = False):
        held = provider(kind).hold_next(respond_now=respond_now)
        hold_id = uuid.uuid4().hex
        with lock:
            holds[hold_id] = held
        return {"hold_id": hold_id}

    def held(hold_id):
        with lock:
            found = holds.get(hold_id)
        if found is None:
            raise HTTPException(404, "unknown hold")
        return found

    @app.post("/holds/{hold_id}/release")
    def release(hold_id: str):
        held(hold_id).release()
        return {"released": True}

    @app.get("/holds/{hold_id}")
    def hold_state(hold_id: str):
        return {"committed": held(hold_id).committed.is_set()}

    @app.post("/payments/reveal")
    def reveal():
        provider("payments").reveal_hidden_effects()
        return {"ok": True}

    @app.post("/crm/reindex")
    def reindex():
        provider("crm").reindex()
        return {"ok": True}

    return app
