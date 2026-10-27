"""Qualification connector contracts (time-scaled, mock providers only).

Same shapes as the v2 mock contracts (core.connector_profiles.
LIMITED_PROFILES): the same idempotency, lookup semantics, budgets,
side-effect classes and F16 limits. Only the windows are scaled so one
scenario settles in seconds. Every arm (SafeAct and B0-B3) reads these
same values: the baselines get the same connector capabilities.

  q-v2            payments: native key (24 h retention), strong lookup
  q-v2-eventual   payments: native key, eventual lookup
  q-v2-short      payments: native key with a 6 s retention (F09)
  q-v2-inflight   payments/CRM/email: 6 s request deadline, 3 s worker lease (F06)
  q-v2 (crm)      CRM: no native key, eventual lookup
  q-v2 (email)    email: no key, no lookup
"""

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


def _ms(milliseconds):
    return timedelta(milliseconds=milliseconds)


REQUEST_DEADLINE = _s(1)
INVESTIGATION_WINDOW = _s(8)
CHECK_INTERVAL = _s(1)
HORIZON = _s(20)
LEASE_SECONDS = 3

LIMITS = ConnectorLimits(
    backoff_base=_ms(200), backoff_cap=_ms(1000),
    lookup_interval=_ms(300), lookup_interval_cap=_ms(1500),
    dispatch_rate_per_second=50, dispatch_burst=50,
    breaker_threshold=5, breaker_cooldown=_ms(2000),
)

_TIMINGS = dict(
    request_deadline=REQUEST_DEADLINE, investigation_window=INVESTIGATION_WINDOW,
    check_interval=CHECK_INTERVAL, horizon=HORIZON, limits=LIMITS,
)

QUAL_PROFILES = (
    payments_profile("q-v2", safety_margin=_s(2), **_TIMINGS),
    payments_profile("q-v2-eventual", lookup=LookupSemantics.EVENTUAL,
                     safety_margin=_s(2), **_TIMINGS),
    payments_profile("q-v2-short", retention=_s(6), safety_margin=_s(2),
                     **_TIMINGS),
    crm_profile("q-v2", **_TIMINGS),
    payments_profile("q-v2-inflight", safety_margin=_s(8),
                     **dict(_TIMINGS, request_deadline=_s(6))),
    payments_profile("q-v2-eventual-inflight", lookup=LookupSemantics.EVENTUAL,
                     safety_margin=_s(8), **dict(_TIMINGS, request_deadline=_s(6))),
    crm_profile("q-v2-inflight", **dict(_TIMINGS, request_deadline=_s(6))),
    email_profile("q-v2", request_deadline=REQUEST_DEADLINE, limits=LIMITS),
    email_profile("q-v2-inflight", request_deadline=_s(6), limits=LIMITS),
)

# Mock-provider settings that must agree with the contracts above.
PAYMENTS_SHORT_RETENTION_SECONDS = 6


def profile(connector_id, version):
    for found in QUAL_PROFILES:
        if found.connector_id == connector_id and found.version == version:
            return found
    raise KeyError((connector_id, version))
