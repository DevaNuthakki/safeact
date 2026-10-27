"""Boundary timing for time-dependent tests (database clock).

Rule: assert clearly before a boundary or clearly after it, never at
the edge. The margin on each side is at least 1 s or 25 % of the
window, whichever is larger. Positions come from database timestamps
(clock_timestamp()), not from when the test started. Not a test module.
"""

import time

from datetime import timedelta


def margin_for(window):
    """Required distance from a boundary for a window of this size."""

    seconds = window.total_seconds() if isinstance(window, timedelta) \
        else float(window)
    return timedelta(seconds=max(1.0, 0.25 * seconds))


def db_now(connection):
    now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
    return now


# Test windows are seconds long; a longer wait means a wrong boundary.
MAX_WAIT_SECONDS = 30


def wait_until_past(connection, boundary, margin):
    """Sleep until the database clock is at least boundary + margin."""

    target = boundary + margin
    while True:
        remaining = (target - db_now(connection)).total_seconds()
        if remaining <= 0:
            return
        if remaining > MAX_WAIT_SECONDS:
            raise AssertionError(
                f"boundary is {remaining:.0f} s away; not a test window"
            )
        time.sleep(min(remaining, 0.5) + 0.01)


def assert_clearly_before(test, connection, boundary, margin):
    """Fail if the database clock is not at least margin before boundary."""

    now = db_now(connection)
    test.assertLessEqual(
        now, boundary - margin,
        f"too close to the boundary: {boundary - now} left",
    )
