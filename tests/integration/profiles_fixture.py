"""Test-scaled connector contract versions (disposable schemas only).

Same profile shapes as the mock profiles, with second-scale windows so
boundaries can be crossed in a test run. They are registered only in
disposable schemas, never in public. Not a test module.
"""

from datetime import timedelta

from core.connector_profiles import (
    crm_profile,
    email_profile,
    payments_profile,
)
from core.contracts import LookupSemantics


S = lambda seconds: timedelta(seconds=seconds)  # noqa: E731

# CRM: investigation window 6 s, post-escalation pass every 2 s for 20 s.
CRM_FAST = crm_profile(
    "t-fast", request_deadline=S(1), investigation_window=S(6),
    check_interval=S(2), horizon=S(20),
)

# Payments, eventual lookup, same short investigation timing.
PAYMENTS_EVENTUAL_FAST = payments_profile(
    "t-eventual-fast", lookup=LookupSemantics.EVENTUAL,
    request_deadline=S(1), investigation_window=S(6),
    check_interval=S(2), horizon=S(20),
)

TEST_PROFILES = (CRM_FAST, PAYMENTS_EVENTUAL_FAST)

# Payments, strong lookup, short investigation timing.
PAYMENTS_FAST = payments_profile(
    "t-fast", request_deadline=S(1), investigation_window=S(6),
    check_interval=S(2), horizon=S(20),
)

# CRM with a short post-escalation pass, for reopen tests: window 2 s,
# one lookup per 2 s, none 6 s after escalation, at most 2 reopens.
CRM_REOPEN = crm_profile(
    "t-reopen", request_deadline=S(1), investigation_window=S(2),
    check_interval=S(2), horizon=S(6), max_reopens=2,
)

TEST_PROFILES = TEST_PROFILES + (PAYMENTS_FAST, CRM_REOPEN)

# Payments, eventual lookup, short key retention: cutoff for a
# protected same-key retry is first fence + 10 s - 3 s = 7 s. The 3 s
# margin covers the 2 s request deadline. Investigation stays open
# (30 s) so the retry decision and the fence can straddle the cutoff.
PAYMENTS_RETENTION = payments_profile(
    "t-retention", lookup=LookupSemantics.EVENTUAL,
    retention=S(10), safety_margin=S(3), request_deadline=S(2),
    investigation_window=S(30), check_interval=S(2), horizon=S(20),
)

# A request deadline longer than the 1 s worker lease, so a lease can
# expire while the original request is still running (F06).
PAYMENTS_SLOW = payments_profile(
    "t-slow", request_deadline=S(4), investigation_window=S(30),
    check_interval=S(2), horizon=S(20),
)
CRM_SLOW = crm_profile(
    "t-slow", request_deadline=S(4), investigation_window=S(30),
    check_interval=S(2), horizon=S(20),
)
EMAIL_SLOW = email_profile("t-slow", request_deadline=S(4))

TEST_PROFILES = TEST_PROFILES + (
    PAYMENTS_RETENTION, PAYMENTS_SLOW, CRM_SLOW, EMAIL_SLOW,
)
