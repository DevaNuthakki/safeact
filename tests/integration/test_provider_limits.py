"""F16 (0018): 429s, long outages and retry storms, against PostgreSQL.

Disposable schema, in-process mock providers, the real ExecutionWorker,
relay and Reconciler. Contracts use time-scaled limits so a run takes
seconds. Asserted:
- a rate-limited dispatch backs off: RETRY_ELIGIBLE with a recorded
  not_before, refused (SAT30) and left unclaimable until then, never
  shorter than Retry-After; the jitter is deterministic and bounded;
- the breaker opens after consecutive failing outcomes, refuses claims
  (SAT31) without appending anything, admits one half-open probe after
  the cooldown and closes on a decisive outcome; every change recorded;
- the token bucket bounds claims per second;
- a long outage: lookups are paced (bounded count, the database refuses
  an early lookup), every failed lookup is kept as evidence, and the
  operation escalates at its deadline;
- a CRM 429 is ambiguous ('ambiguous_rate_limited') and counts as a
  provider failure;
- a v1 contract (no limits row) behaves exactly as before;
- a retry storm of 300 operations: provider calls stay within the rate
  bound, no duplicate effect, every intent accounted for, and every
  reclaim happened at or after its not_before.
"""

import os
import time
import unittest

from datetime import timedelta
from uuid import uuid4

import psycopg

from psycopg import sql

from connectors.crm import CrmConnector
from connectors.crm_mock import MockCrmProvider
from connectors.email import EmailConnector
from connectors.email_mock import MockEmailProvider
from connectors.ledger import EffectsLedger
from connectors.payments import PaymentsConnector
from connectors.payments_mock import MockPaymentsProvider
from core.connector_profiles import (
    ConnectorLimits,
    crm_profile,
    payments_profile,
)
from core.lifecycle import LifecycleState as S
from storage import durable_lifecycle as L
from storage.experimental_durable_execution import (
    ExecutionWorker,
    InMemoryTaskQueue,
    relay_once,
)
from storage.experimental_recovery_worker import Reconciler
from tests.integration.lifecycle_fixture import (
    DisposableLifecycleDatabase,
    commit,
)


ENABLED = (
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1"
)


def ms(value):
    return timedelta(milliseconds=value)


BACKOFF = payments_profile(
    "t-backoff", request_deadline=timedelta(seconds=1),
    investigation_window=timedelta(seconds=30),
    limits=ConnectorLimits(
        backoff_base=ms(300), backoff_cap=ms(900),
        lookup_interval=ms(200), lookup_interval_cap=ms(800),
    ),
)
BREAKER = payments_profile(
    "t-breaker", request_deadline=timedelta(seconds=1),
    investigation_window=timedelta(seconds=30),
    limits=ConnectorLimits(
        backoff_base=ms(100), backoff_cap=ms(200),
        lookup_interval=ms(200), lookup_interval_cap=ms(800),
        breaker_threshold=3, breaker_cooldown=ms(1500),
    ),
)
RATE = payments_profile(
    "t-rate", request_deadline=timedelta(seconds=1),
    investigation_window=timedelta(seconds=30),
    limits=ConnectorLimits(
        backoff_base=ms(100), backoff_cap=ms(200),
        lookup_interval=ms(200), lookup_interval_cap=ms(800),
        dispatch_rate_per_second=2, dispatch_burst=2,
    ),
)
# No native key: an outage cannot be retried safely, so it is paced and
# escalated (a native-key provider retries under backoff instead).
OUTAGE = crm_profile(
    "t-outage", request_deadline=timedelta(seconds=1),
    investigation_window=timedelta(seconds=4),
    limits=ConnectorLimits(
        backoff_base=ms(100), backoff_cap=ms(200),
        lookup_interval=ms(300), lookup_interval_cap=ms(1200),
    ),
)
# A native key: an outage is retried under backoff until the attempt
# budget is spent, then waits (paced) for the deadline and escalates.
NATIVE_OUTAGE = payments_profile(
    "t-native-outage", request_deadline=timedelta(seconds=1),
    investigation_window=timedelta(seconds=5),
    limits=ConnectorLimits(
        backoff_base=ms(100), backoff_cap=ms(300),
        lookup_interval=ms(300), lookup_interval_cap=ms(1200),
    ),
)
CRM_LIMITED = crm_profile(
    "t-limits", request_deadline=timedelta(seconds=1),
    investigation_window=timedelta(seconds=30),
    limits=ConnectorLimits(
        backoff_base=ms(100), backoff_cap=ms(200),
        lookup_interval=ms(200), lookup_interval_cap=ms(800),
        breaker_threshold=5, breaker_cooldown=ms(1000),
    ),
)
STORM = payments_profile(
    "t-storm", request_deadline=timedelta(seconds=1),
    investigation_window=timedelta(seconds=60),
    limits=ConnectorLimits(
        backoff_base=ms(200), backoff_cap=ms(1000),
        lookup_interval=ms(100), lookup_interval_cap=ms(400),
        dispatch_rate_per_second=100, dispatch_burst=20,
    ),
)
PROFILES = (BACKOFF, BREAKER, RATE, OUTAGE, NATIVE_OUTAGE, CRM_LIMITED,
            STORM)


@unittest.skipUnless(ENABLED, "Explicit disposable PostgreSQL opt-in required")
class ProviderLimitTests(unittest.TestCase):

    def setUp(self):
        # One schema per test: the relay publishes every claimable
        # operation in a schema, so another test's leftovers would use
        # this test's one-shot faults.
        self.db = DisposableLifecycleDatabase()
        self.addCleanup(self.db.cleanup)
        self.db.start()
        self.db.register_profiles(PROFILES)
        self.schema = self.db.schema
        # Fresh providers and provider account per test as well.
        self.ledger = EffectsLedger()
        self.payments = MockPaymentsProvider(ledger=self.ledger)
        self.crm = MockCrmProvider(ledger=self.ledger)
        self.account = "acct-" + uuid4().hex[:12]
        connectors = (
            PaymentsConnector(self.payments), CrmConnector(self.crm),
            EmailConnector(MockEmailProvider(ledger=self.ledger)),
        )
        self.queue = InMemoryTaskQueue()
        self.worker = ExecutionWorker(
            connect=lambda: self.db.connect(self.db.worker_a),
            schema=self.schema, connectors=connectors, lease_seconds=30,
        )
        self.reconciler = Reconciler(
            connect=lambda: self.db.connect(self.db.recoverer),
            schema=self.schema, connectors=connectors, retry_authorized=True,
        )
        self.relay_connection = self.db.connect(self.db.worker_a)
        self.recoverer = self.db.connect(self.db.recoverer)

    # -- helpers -------------------------------------------------------

    def admit(self, profile):
        if profile.connector_id == "mock-crm":
            # The CRM correlates on the operation id (no provider key).
            return self.db.admit(
                request_key="f16-" + uuid4().hex, connector_id="mock-crm",
                contract_version=profile.version,
                action_type="create_replacement_order",
                provider_account_id=self.account,
                payload={"case_id": "case-1", "sku": "sku-1"},
            )
        return self.db.admit(
            request_key="f16-" + uuid4().hex,
            contract_version=profile.version,
            provider_account_id=self.account,
            payload={"charge_id": "ch-" + uuid4().hex[:8], "amount_minor": 1},
        )

    def relay_and_work(self):
        relay_once(self.relay_connection, self.schema, self.queue)
        return self.worker.run_until_idle(self.queue)

    def state(self, operation_id):
        return commit(self.recoverer, lambda: L.current_state(
            self.recoverer, self.schema, operation_id)).state

    def rows(self, query, *args):
        return self.db.admin.execute(sql.SQL(query).format(
            s=sql.Identifier(self.schema)), args).fetchall()

    def schedule(self, operation_id):
        return self.rows(
            "SELECT seq, attempts, backoff_seconds, retry_after_seconds, "
            "not_before, not_before - clock_timestamp() "
            "FROM {s}.safeact_retry_schedule WHERE operation_id = %s "
            "ORDER BY seq", operation_id)

    def claimable(self):
        return {r[0] for r in commit(self.relay_connection, lambda: (
            self.relay_connection.execute(sql.SQL(
                "SELECT * FROM {}.safeact_claimable_operations(1000)"
            ).format(sql.Identifier(self.schema))).fetchall()))}

    def events(self):
        return [r[0] for r in self.rows(
            "SELECT event FROM {s}.safeact_connector_health_events "
            "WHERE provider_account_id = %s ORDER BY created_at",
            self.account)]

    def try_claim(self, operation_id):
        return commit(self.relay_connection, lambda: L.claim(
            self.relay_connection, self.schema, operation_id,
            lease_seconds=30))

    # -- backoff -------------------------------------------------------

    def test_a_rate_limited_dispatch_backs_off(self):
        operation_id = self.admit(BACKOFF)
        self.payments.rate_limit_next = 1
        self.relay_and_work()
        self.assertEqual(self.state(operation_id), S.UNKNOWN)
        facts = self.rows(
            "SELECT details -> 'provider' FROM {s}.safeact_attempt_outcomes "
            "WHERE operation_id = %s", operation_id)
        self.assertEqual(facts[0][0], {"rate_limited": "true"})

        self.reconciler.reconcile_once()
        self.assertEqual(self.state(operation_id), S.RETRY_ELIGIBLE)
        (seq, attempts, backoff, retry_after, not_before, wait), = (
            self.schedule(operation_id))
        self.assertEqual(attempts, 1)
        self.assertIsNone(retry_after)
        self.assertLessEqual(float(backoff), 0.3)
        if wait > timedelta(0):
            self.assertNotIn(operation_id, self.claimable())
            with self.assertRaises(L.NotYetDue):
                self.try_claim(operation_id)
        time.sleep(max(0.0, wait.total_seconds()) + 0.05)

        self.relay_and_work()
        self.assertEqual(self.state(operation_id), S.SUCCEEDED)
        key = self.db.provider_key(operation_id)
        self.assertEqual(len(self.ledger.effects_for(key)), 1)
        self.assertEqual(len(self.ledger.calls_for(key)), 2)
        reclaimed = self.rows(
            "SELECT created_at FROM {s}.safeact_operation_transitions "
            "WHERE operation_id = %s AND seq = %s", operation_id, seq + 1)
        self.assertGreaterEqual(reclaimed[0][0], not_before)

    def test_retry_after_is_never_cut_short(self):
        operation_id = self.admit(BACKOFF)
        self.payments.rate_limit_next = 1
        self.payments.retry_after = 1.25
        self.relay_and_work()
        self.reconciler.reconcile_once()
        (_, _, backoff, retry_after, _, wait), = self.schedule(operation_id)
        self.assertEqual(float(retry_after), 1.25)
        self.assertGreater(wait, timedelta(seconds=1.0))
        self.assertLessEqual(float(backoff), 0.3)
        with self.assertRaises(L.NotYetDue):
            self.try_claim(operation_id)

    def test_jitter_is_deterministic_and_bounded(self):
        operation_id = self.admit(BACKOFF)
        values = []
        for attempts in range(1, 8):
            first, second = (self.rows(
                "SELECT {s}.safeact_backoff_seconds(%s, %s)",
                operation_id, attempts)[0][0] for _ in range(2))
            self.assertEqual(first, second)
            ceiling = min(0.9, 0.3 * 2 ** (attempts - 1))
            self.assertGreaterEqual(float(first), 0)
            self.assertLessEqual(float(first), ceiling)
            values.append(first)
        self.assertGreater(len(set(values)), 1)

    # -- breaker and rate --------------------------------------------------

    def test_the_breaker_opens_and_admits_one_probe(self):
        self.payments.outage = True
        failing = [self.admit(BREAKER) for _ in range(3)]
        self.relay_and_work()
        for operation_id in failing:
            self.assertEqual(self.state(operation_id), S.UNKNOWN)
        self.assertEqual(self.events(), ["breaker_opened"])

        waiting = [self.admit(BREAKER) for _ in range(2)]
        self.assertFalse(set(waiting) & self.claimable())
        with self.assertRaises(L.ProviderLimited):
            self.try_claim(waiting[0])
        self.assertEqual(self.state(waiting[0]), S.RECORDED)

        time.sleep(1.6)
        self.payments.outage = False
        probe = self.try_claim(waiting[0])
        with self.assertRaises(L.ProviderLimited):
            self.try_claim(waiting[1])
        fence = commit(self.relay_connection, lambda: L.begin_dispatch(
            self.relay_connection, self.schema, probe, lease_seconds=30))
        self.db.prove(self.relay_connection, fence.attempt_id, applied=True)
        commit(self.relay_connection, lambda: L.record_dispatch_outcome(
            self.relay_connection, self.schema, fence, S.SUCCEEDED,
            reason="provider_confirmed"))
        self.assertEqual(self.events(),
                         ["breaker_opened", "probe_admitted", "breaker_closed"])
        self.try_claim(waiting[1])

    def test_the_dispatch_rate_bounds_claims(self):
        operations = [self.admit(RATE) for _ in range(5)]
        claimed, refused = [], []
        for operation_id in operations:
            try:
                self.try_claim(operation_id)
                claimed.append(operation_id)
            except L.ProviderLimited:
                refused.append(operation_id)
        self.assertEqual(len(claimed), 2)
        for operation_id in refused:
            self.assertEqual(self.state(operation_id), S.RECORDED)
        time.sleep(0.6)
        self.try_claim(refused[0])
        with self.assertRaises(L.ProviderLimited):
            self.try_claim(refused[1])

    # -- long outage and lookup pacing ------------------------------------

    def test_a_long_outage_paces_lookups_and_escalates_with_evidence(self):
        self.crm.outage = True
        operation_id = self.admit(OUTAGE)
        self.relay_and_work()
        self.assertEqual(self.state(operation_id), S.UNKNOWN)

        self.reconciler.reconcile_once()          # first lookup: due at once
        self.assertEqual(self.state(operation_id), S.UNKNOWN)
        snap = commit(self.recoverer, lambda: L.recovery_snapshot(
            self.recoverer, self.schema, operation_id))
        with self.assertRaises(L.NotYetDue):
            commit(self.recoverer, lambda: L.recovery_transition(
                self.recoverer, self.schema, operation_id,
                expected_seq=snap.seq, expected_state=S.UNKNOWN,
                to_state=S.VERIFYING, reason="too_early"))

        passes, started = 0, time.monotonic()
        while (self.state(operation_id) is not S.ESCALATED_UNRESOLVED
               and time.monotonic() - started < 10):
            self.reconciler.reconcile_once()
            passes += 1
            time.sleep(0.05)
        self.assertEqual(self.state(operation_id), S.ESCALATED_UNRESOLVED)

        lookups = self.rows(
            "SELECT details ->> 'note' FROM {s}.safeact_attempt_outcomes "
            "WHERE operation_id = %s "
            "AND details ->> 'source' = 'reconciler_lookup'", operation_id)
        # Due at 0, then +0.3, +0.6, +1.2, +1.2 ... until the 4 s deadline:
        # at most 6 lookups, against ~80 passes.
        self.assertGreater(passes, 30)
        self.assertLessEqual(len(lookups), 7)
        self.assertGreaterEqual(len(lookups), 3)
        self.assertTrue(all(note == "lookup_unavailable"
                            for (note,) in lookups))
        # One CRM call (no native key: never resent), no effect.
        self.assertEqual(self.ledger.effects_for(str(operation_id)), ())
        self.assertEqual(len(self.ledger.calls_for(str(operation_id))), 1)

    def test_a_native_key_outage_retries_under_backoff_then_escalates(self):
        self.payments.outage = True
        operation_id = self.admit(NATIVE_OUTAGE)
        started = time.monotonic()
        while (self.state(operation_id) is not S.ESCALATED_UNRESOLVED
               and time.monotonic() - started < 20):
            self.relay_and_work()
            self.reconciler.reconcile_once()
            time.sleep(0.05)
        self.assertEqual(self.state(operation_id), S.ESCALATED_UNRESOLVED)
        schedule = self.schedule(operation_id)
        # max_attempts 3: two retries, each with a recorded backoff.
        self.assertEqual([row[1] for row in schedule], [1, 2])
        key = self.db.provider_key(operation_id)
        self.assertEqual(len(self.ledger.calls_for(key)), 3)
        self.assertEqual(self.ledger.effects_for(key), ())

    def test_a_crm_rate_limit_is_ambiguous_and_a_provider_failure(self):
        operation_id = self.admit(CRM_LIMITED)
        self.crm.rate_limit_next = 1
        self.relay_and_work()
        self.assertEqual(self.state(operation_id), S.UNKNOWN)
        reasons = [r[0] for r in self.rows(
            "SELECT reason FROM {s}.safeact_operation_transitions "
            "WHERE operation_id = %s ORDER BY seq", operation_id)]
        self.assertEqual(reasons[-1], "ambiguous_rate_limited")
        failures = self.rows(
            "SELECT consecutive_failures FROM {s}.safeact_connector_health "
            "WHERE provider_account_id = %s", self.account)
        self.assertEqual(failures, [(1,)])

    def test_a_v1_contract_has_no_limits(self):
        operation_id = self.db.admit(request_key="v1-" + uuid4().hex)
        pacing = commit(self.recoverer, lambda: L.pacing(
            self.recoverer, self.schema, operation_id))
        self.assertFalse(pacing.limited)
        self.assertIsNone(pacing.next_lookup_at)
        self.payments.rate_limit_next = 1
        self.relay_and_work()
        self.reconciler.reconcile_once()
        self.assertEqual(self.state(operation_id), S.RETRY_ELIGIBLE)
        self.assertEqual(self.schedule(operation_id), [])
        self.try_claim(operation_id)     # due at once: no backoff declared

    # -- retry storm ---------------------------------------------------------

    def test_a_retry_storm_stays_bounded_and_loses_nothing(self):
        count = 300
        self.payments.rate_limit_next = 200
        operations = [self.admit(STORM) for _ in range(count)]
        started = time.monotonic()
        terminal = {S.SUCCEEDED, S.FAILED_CONFIRMED, S.ESCALATED_UNRESOLVED}
        while time.monotonic() - started < 120:
            self.relay_and_work()
            self.reconciler.reconcile_once(limit=1000)
            states = self.rows(
                "SELECT p.state, count(*) FROM {s}.safeact_state_projection p "
                "JOIN {s}.safeact_operations o USING (operation_id) "
                "WHERE o.provider_account_id = %s GROUP BY 1", self.account)
            if sum(n for state, n in states if S(state) in terminal) == count:
                break
            time.sleep(0.05)
        elapsed = time.monotonic() - started

        by_state = {S(state): n for state, n in states}
        self.assertEqual(sum(by_state.values()), count)        # A5
        calls = sum(len(self.ledger.calls_for(self.db.provider_key(op)))
                    for op in operations)
        effects = [len(self.ledger.effects_for(self.db.provider_key(op)))
                   for op in operations]
        self.assertLessEqual(max(effects), 1)                  # no duplicate
        self.assertEqual(sum(effects), by_state.get(S.SUCCEEDED, 0))
        # Token bucket: burst + rate * elapsed (+1 for rounding).
        self.assertLessEqual(calls, 20 + 100 * elapsed + 1)
        early = self.rows("""
            SELECT count(*) FROM {s}.safeact_retry_schedule r
            JOIN {s}.safeact_operation_transitions t
              ON t.operation_id = r.operation_id AND t.seq = r.seq + 1
            WHERE t.to_state = 'CLAIMED' AND t.created_at < r.not_before""")
        self.assertEqual(early, [(0,)])
        self.assertGreater(by_state.get(S.SUCCEEDED, 0), count // 2)
        print(f"\nretry storm: {by_state} calls={calls} "
              f"elapsed={elapsed:.1f}s", flush=True)


if __name__ == "__main__":
    unittest.main()
