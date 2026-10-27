"""Connector contracts for the agent evaluation environment (local only).

The real contract shapes (core.connector_profiles) with evaluation-scaled
windows, so a scenario resolves or escalates within a trial:

- payments v1: a short investigation, so a lost response is verified
  quickly.
- payments v1-short-retention: the same payments contract for a
  provider account whose idempotency keys are kept only a few seconds.
  It is declared truthfully (the scripted provider forgets those keys
  after exactly that retention), so the key-expiry scenario tests
  SafeAct under its declared contract, never a provider that lies.
  Its protected-retry window (retention - margin) closes before the
  first verification, so SafeAct can never retry it under the key.
- crm v1: a long investigation, so a caller has time to propose a
  reviewed retry before the deadline.
- email v1: no lookup, so any ambiguity escalates at once.

Registered only in the disposable evaluation schema, never in public.
"""

from dataclasses import replace
from datetime import timedelta

from core.connector_profiles import crm_profile, email_profile, payments_profile


def _s(seconds):
    return timedelta(seconds=seconds)


PACE = "agent-eval"
SHORT_RETENTION_VERSION = "v1-short-retention"

# Seconds. (request deadline, investigation window, check interval,
# post-escalation horizon)
PAYMENTS_TIMINGS = (2, 8, 2, 60)
CRM_TIMINGS = (2, 30, 3, 60)
EMAIL_DEADLINE = 2
SHORT_RETENTION = 4
SHORT_RETENTION_MARGIN = 3


def _timings(values):
    deadline, window, interval, horizon = (_s(v) for v in values)
    return dict(
        request_deadline=deadline, investigation_window=window,
        check_interval=interval, horizon=horizon,
    )


def _labelled(profile):
    return replace(
        profile,
        contract_reference=f"{profile.contract_reference} "
                           f"({PACE}-scaled timings, local evaluation only)",
    )


AGENT_PROFILES = tuple(_labelled(p) for p in (
    payments_profile("v1", safety_margin=_s(5), **_timings(PAYMENTS_TIMINGS)),
    payments_profile(
        SHORT_RETENTION_VERSION, retention=_s(SHORT_RETENTION),
        safety_margin=_s(SHORT_RETENTION_MARGIN), **_timings(PAYMENTS_TIMINGS),
    ),
    crm_profile("v1", **_timings(CRM_TIMINGS)),
    email_profile("v1", request_deadline=_s(EMAIL_DEADLINE)),
))
