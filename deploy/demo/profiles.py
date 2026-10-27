"""Time-scaled connector contracts for the local demo and the E2E tests.

Same contract shapes as core.connector_profiles.MOCK_PROFILES (the same
connectors, versions, idempotency, lookup semantics, budgets and side
effects); only the windows are shorter, so an investigation, an
escalation and a reviewer's decision fit into a live walkthrough or a
test run. Each contract's reference says so, and the operation detail
view shows the windows actually in force. Registered only in
safeact_demo and disposable schemas, never in public.
"""

from dataclasses import replace
from datetime import timedelta

from core.connector_profiles import (
    ConnectorLimits,
    crm_profile,
    email_profile,
    payments_profile,
)
from core.contracts import LookupSemantics


def _s(seconds):
    return timedelta(seconds=seconds)


# (request deadline, investigation window, check interval, horizon)
PACES = {
    # A person has a minute to look before work escalates.
    "demo": (2, 60, 10, 600),
    # Fast enough for an end-to-end test run.
    "e2e": (1, 6, 1, 30),
}


def _ms(milliseconds):
    return timedelta(milliseconds=milliseconds)


# F16 limits (0018) at the same pace, so retries back off, lookups are
# paced and the console's "next check" shows real times.
# (backoff base, cap, lookup interval, cap, rate/s, breaker cooldown), ms.
PACE_LIMITS = {
    "demo": (1000, 10000, 5000, 20000, 25, 10000),
    "e2e": (200, 1000, 500, 2000, 50, 2000),
}


def scaled_limits(pace):
    base, cap, interval, interval_cap, rate, cooldown = PACE_LIMITS[pace]
    return ConnectorLimits(
        backoff_base=_ms(base), backoff_cap=_ms(cap),
        lookup_interval=_ms(interval), lookup_interval_cap=_ms(interval_cap),
        dispatch_rate_per_second=rate, dispatch_burst=rate,
        breaker_threshold=5, breaker_cooldown=_ms(cooldown),
    )


def scaled_profiles(pace):
    deadline, window, interval, horizon = (_s(v) for v in PACES[pace])
    timings = dict(
        request_deadline=deadline, investigation_window=window,
        check_interval=interval, horizon=horizon,
    )
    limits = scaled_limits(pace)
    profiles = (
        payments_profile("v1", safety_margin=_s(5), limits=limits, **timings),
        payments_profile(
            "v1-eventual", lookup=LookupSemantics.EVENTUAL,
            safety_margin=_s(5), limits=limits, **timings,
        ),
        crm_profile("v1", limits=limits, **timings),
        email_profile("v1", request_deadline=deadline, limits=limits),
    )
    return tuple(_labelled(p, pace) for p in profiles)


def _labelled(profile, pace):
    return replace(
        profile,
        contract_reference=f"{profile.contract_reference} "
                           f"({pace}-scaled timings, local only)",
    )


DEMO_PROFILES = scaled_profiles("demo")
E2E_PROFILES = scaled_profiles("e2e")
