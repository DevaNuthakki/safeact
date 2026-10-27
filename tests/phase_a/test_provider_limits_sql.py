"""Static checks on F16 limits (0018) and ConnectorLimits."""

import unittest

from datetime import timedelta

from core.connector_profiles import (
    LIMITED_PROFILES,
    LIMITS_COLUMNS,
    MOCK_PROFILES,
    PAYMENTS_LIMITS,
    STRIPE_LIMITS,
    ConnectorLimits,
    scaled_limits,
    stripe_profile,
)
from core.lifecycle import AMBIGUOUS_REASONS
from storage import durable_lifecycle_sql as lsql
from storage.lifecycle_sql import v0003, v0018


SCHEMA = "safeact_it_" + "0" * 32


def render(statement):
    return statement.as_string(None)


class LimitsTests(unittest.TestCase):

    def test_decision_four_values(self):
        self.assertEqual(PAYMENTS_LIMITS.backoff_base, timedelta(seconds=1))
        self.assertEqual(PAYMENTS_LIMITS.backoff_cap, timedelta(seconds=60))
        self.assertEqual(PAYMENTS_LIMITS.dispatch_rate_per_second, 25)
        self.assertEqual(PAYMENTS_LIMITS.breaker_threshold, 5)
        self.assertEqual(PAYMENTS_LIMITS.breaker_cooldown,
                         timedelta(seconds=30))
        crm = next(p for p in LIMITED_PROFILES if p.connector_id == "mock-crm")
        self.assertEqual(crm.limits.dispatch_rate_per_second, 5)
        self.assertLess(STRIPE_LIMITS.dispatch_rate_per_second, 25)
        self.assertIs(stripe_profile().limits, STRIPE_LIMITS)

    def test_v1_contracts_declare_no_limits(self):
        for profile in MOCK_PROFILES:
            with self.subTest(profile=profile.version):
                self.assertIsNone(profile.limits)

    def test_invalid_limits_are_refused(self):
        base = dict(backoff_base=timedelta(seconds=1),
                    backoff_cap=timedelta(seconds=2),
                    lookup_interval=timedelta(seconds=1),
                    lookup_interval_cap=timedelta(seconds=2))
        for change in (
            {"backoff_cap": timedelta(milliseconds=500)},
            {"lookup_interval_cap": timedelta(milliseconds=500)},
            {"dispatch_rate_per_second": 5},
            {"dispatch_rate_per_second": 0, "dispatch_burst": 1},
            {"breaker_threshold": 3},
            {"breaker_threshold": 0, "breaker_cooldown": timedelta(1)},
            {"backoff_base": timedelta(0)},
            {"backoff_base": timedelta(microseconds=1500)},
        ):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    ConnectorLimits(**{**base, **change})

    def test_scaling_and_rows(self):
        scaled = scaled_limits(PAYMENTS_LIMITS, 0.1)
        self.assertEqual(scaled.backoff_base, timedelta(milliseconds=100))
        self.assertEqual(scaled.dispatch_rate_per_second, 250)
        row = PAYMENTS_LIMITS.row("mock-payments", "v2")
        self.assertEqual(tuple(row), LIMITS_COLUMNS)


class Sql0018Tests(unittest.TestCase):

    def setUp(self):
        self.s = v0003._schema(SCHEMA)

    def test_vocabulary_matches_core(self):
        self.assertEqual(set(lsql.AMBIGUOUS_REASONS), set(AMBIGUOUS_REASONS))
        self.assertIn("ambiguous_rate_limited",
                      render(v0018._dispatch_outcome(self.s)))

    def test_claim_runs_the_gate_before_appending(self):
        text = render(v0018._claim(self.s))
        gate = text.index("safeact_dispatch_gate(p_operation, v_retry)")
        append = text.index("safeact_lifecycle_append(")
        self.assertLess(gate, append)

    def test_append_paces_before_insert_and_schedules_after(self):
        text = render(v0018._append(self.s))
        pacing = text.index("safeact_next_lookup_at(p_operation) >")
        insert = text.index(".safeact_operation_transitions (")
        schedule = text.index(".safeact_retry_schedule (")
        audit = text.index(".safeact_audit_events (")
        self.assertLess(pacing, insert)
        self.assertLess(insert, schedule)
        self.assertLess(schedule, audit)
        self.assertEqual(text.count("safeact_state_projection"), 1)

    def test_scans_honour_backoff_and_breaker(self):
        for statement in (v0018._claimable_function, v0018._relay):
            text = render(statement(self.s))
            with self.subTest(text=text[:60]):
                self.assertIn("r.not_before > clock_timestamp()", text)
                self.assertIn("h.open_until > clock_timestamp()", text)

    def test_failing_reasons_never_include_key_conflicts(self):
        for reason in ("ambiguous_key_conflict", "provider_in_flight_conflict",
                       "request_deadline_elapsed_before_send",
                       "lease_expired_investigate"):
            self.assertNotIn(reason, v0018.FAILING_REASONS)

    def test_new_sqlstates_are_registered(self):
        self.assertEqual((lsql.NOT_YET_DUE, lsql.PROVIDER_LIMITED),
                         ("SAT30", "SAT31"))


if __name__ == "__main__":
    unittest.main()
