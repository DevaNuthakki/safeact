"""Late effects, outstanding processes and reporting denominator counterexamples."""

import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chaos import coverage, oracle, qualify, report, scoring
from chaos.arms.base import Admission, OperationView
from chaos.provider_trace import ObservedProvider
from chaos.quiescence import ProviderBarrier, process_group_exists, stop_processes
from chaos.runner import FaultDriver, Timeline, run
from chaos.scenarios import OperationSpec, Scenario
from connectors.ledger import EffectsLedger
from connectors.mock_support import HeldCall
from connectors.payments_mock import MockPaymentsProvider, ProviderTimeout


def result(*, expected=1, completed=True, achievable=True, **changes):
    row = dict(scenario_id="D-CONTROL-0000", arm="safeact", family="CONTROL",
        connector="payments", expected_operations=expected, completion_achievable=achievable,
        operations=[dict(index=0, outcome_class="confirmed_success" if completed else "confirmed_failure",
            accepted_operations=1, effects=int(completed), evidence_rows=1,
            safe_completion=completed, resolution_seconds=1.0)],
        prohibited_duplicates=0, orphan_effects=0, unaccounted=0, changed_accepted=0,
        false_claims=0, harness_errors=[], integrity=[], cleanup_incomplete=False)
    row.update(changes)
    return row


class BarrierTests(unittest.TestCase):
    def test_closed_admission_blocks_effects_but_existing_call_can_finish(self):
        barrier, entered, release = ProviderBarrier(), threading.Event(), threading.Event()
        ledger = EffectsLedger()
        raw = MockPaymentsProvider(ledger=ledger)
        def before(*args):
            entered.set()
            release.wait(2)
        raw.before_call = before
        provider = ObservedProvider("payments", raw, ledger.record_observation, barrier=barrier)
        thread = threading.Thread(target=lambda: provider.refund("first", {"amount": 10}))
        thread.start()
        self.addCleanup(release.set)
        self.addCleanup(thread.join, 2)
        try:
            self.assertTrue(entered.wait(1))
            barrier.close()
            with self.assertRaisesRegex(RuntimeError, "admission is closed"):
                provider.refund("late", {"amount": 10})
            with self.assertRaisesRegex(RuntimeError, "drain deadline"):
                barrier.wait_idle(0)
        finally:
            release.set()
            thread.join(2)
        barrier.wait_idle(1)
        self.assertEqual(len(ledger.effects_for("first")), 1)
        self.assertEqual(ledger.effects_for("late"), ())

    def test_failed_call_does_not_leave_a_false_active_request(self):
        barrier = ProviderBarrier()
        with self.assertRaises(ValueError):
            with barrier.request():
                raise ValueError("failed request")
        barrier.close()
        barrier.wait_idle(0)

    def test_open_admission_cannot_establish_quiescence(self):
        with self.assertRaisesRegex(RuntimeError, "Close provider admission"):
            ProviderBarrier().wait_idle(0)

    def test_unused_hold_does_not_wait_for_a_nonexistent_commit(self):
        driver = FaultDriver(None, Timeline())
        held = HeldCall()
        driver.holds.append(held)
        driver.cancel()
        self.assertFalse(held._release.is_set())
        driver.release_holds()
        self.assertEqual(driver.wait_holds(0), dict(consumed_holds=0, unused_holds=1))

    def test_unfinished_consumed_hold_is_a_cleanup_error(self):
        driver = FaultDriver(None, Timeline())
        held = HeldCall()
        held.started.set()
        driver.holds.append(held)
        with self.assertRaisesRegex(RuntimeError, "did not commit"):
            driver.wait_holds(0)

    def test_hold_release_waits_for_acceptance_instead_of_queue_time(self):
        held, waiting = HeldCall(), threading.Event()
        original_wait = held.started.wait
        def wait(timeout):
            waiting.set()
            return original_wait(timeout)
        slot = SimpleNamespace(mocks={"payments": SimpleNamespace(hold_next=lambda **kw: held)}, proxies={})
        driver = FaultDriver(slot, Timeline())
        with patch.object(held.started, "wait", side_effect=wait):
            try:
                driver.apply(dict(kind="hold", provider="payments", release_ms=0))
                self.assertTrue(waiting.wait(1))
                self.assertFalse(held._release.is_set())
                held.started.set()
                self.assertTrue(held._release.wait(1))
            finally:
                driver.cancel()

    @unittest.skipUnless(os.name == "posix", "Process groups require POSIX")
    def test_stop_reaps_a_real_process_group(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.addCleanup(child.wait, 5)
        self.addCleanup(child.kill)
        stack = SimpleNamespace(processes={"worker": child}, stop=lambda: None)
        stopped = stop_processes(stack)
        self.assertEqual(stopped, [dict(pid=child.pid, returncode=-signal.SIGKILL)])
        self.assertFalse(process_group_exists(child.pid))

    def test_children_are_checked_even_if_parent_already_exited(self):
        process = SimpleNamespace(pid=12345, poll=lambda: 0)
        stack = SimpleNamespace(processes={"worker": process}, stop=lambda: None)
        with patch("chaos.quiescence.os.killpg", side_effect=[None, None, ProcessLookupError()]) as kill:
            stop_processes(stack)
        self.assertEqual(kill.call_args_list[0].args, (12345, signal.SIGKILL))
        self.assertEqual(kill.call_count, 3)

    def test_surviving_group_cannot_be_reported_as_stopped(self):
        stack = SimpleNamespace(processes={"worker": SimpleNamespace(pid=12345, poll=lambda: 0)}, stop=lambda: None)
        with patch("chaos.quiescence.os.killpg"), self.assertRaisesRegex(RuntimeError, "still exist"):
            stop_processes(stack, timeout=0)


class RunnerDrainTests(unittest.TestCase):
    def late_run(self, outcome="confirmed_failure", fail_stop=False):
        ledger, barrier, stopped = EffectsLedger(), ProviderBarrier(), threading.Event()
        raw_provider = MockPaymentsProvider(ledger=ledger)
        provider = ObservedProvider("payments", raw_provider, ledger.record_observation, barrier=barrier)
        class Arm:
            name = "safeact"
            def prepare(self, case):
                pass
            def submit(self, *args, **kwargs):
                with self_outer.assertRaises(ProviderTimeout):
                    provider.refund("key", {"charge_id": "ch_test", "amount_minor": 100}, timeout=0)
                return Admission("local")
            def observe(self, ids):
                applied = bool(ledger.effects_for("key"))
                current = "confirmed_success" if applied else outcome
                return {i: OperationView(i, current, "SUCCEEDED" if applied else "UNKNOWN") for i in ids}
            def quiesce(self):
                self_outer.assertFalse(ledger.effects_for("key"))
                if fail_stop:
                    raise RuntimeError("worker still executing")
                stopped.set()
            def check_integrity(self):
                return []
        self_outer = self
        case = Scenario("D-F07-0000", "dev", "F07", "payments", "q-v2", 7,
            (OperationSpec("ch_test", 100),),
            ({"kind": "hold", "provider": "payments", "respond_now": True, "release_ms": 60000},), True, 0.02)
        with tempfile.TemporaryDirectory() as directory:
            slot = SimpleNamespace(index=0, ledger=ledger, mocks={"payments": raw_provider},
                proxies={}, fault_dir=Path(directory), reset=lambda: None,
                close_provider_admission=barrier.close, wait_provider_idle=barrier.wait_idle)
            with patch("chaos.runner.POLL_SECONDS", 0.001), patch("chaos.runner.STABLE_SECONDS", 0):
                raw = run(slot, Arm(), case)
        return case, raw, stopped

    def test_late_cleanup_effect_is_in_final_ledger_and_false_claim_check(self):
        case, raw, stopped = self.late_run()
        self.assertTrue(stopped.is_set())
        self.assertFalse(raw["harness_errors"])
        self.assertTrue(raw["quiescence"]["complete"])
        self.assertEqual(raw["quiescence"]["held_commits_drained"]["consumed_holds"], 1)
        effect = next(e for e in raw["ledger"] if e["event"] == "effect")
        self.assertGreater(effect["monotonic_ns"], raw["observation_finished_monotonic_ns"])
        self.assertEqual(raw["operations"]["local"]["outcome_class"], "confirmed_failure")
        self.assertEqual(raw["final_operations"]["local"]["outcome_class"], "confirmed_success")
        evaluated = oracle.evaluate(case, raw)
        self.assertEqual(evaluated["false_claims"], 1)
        self.assertEqual(qualify.exit_code([evaluated], 1), 1)

    def test_cleanup_success_does_not_inflate_timed_completion(self):
        case, raw, _ = self.late_run("pending")
        self.assertEqual(raw["final_operations"]["local"]["outcome_class"], "confirmed_success")
        evaluated = oracle.evaluate(case, raw)
        self.assertEqual(evaluated["operations"][0]["outcome_class"], "pending")
        self.assertFalse(evaluated["operations"][0]["safe_completion"])

    def test_failed_execution_stop_blocks_exit_but_retains_effect(self):
        case, raw, _ = self.late_run(fail_stop=True)
        self.assertTrue(raw["cleanup_incomplete"])
        self.assertTrue(any("worker still executing" in e for e in raw["harness_errors"]))
        self.assertEqual(sum(e["event"] == "effect" for e in raw["ledger"]), 1)
        self.assertEqual(qualify.exit_code([oracle.evaluate(case, raw)], 1), 1)

    def test_cleanup_effect_does_not_establish_scheduled_late_commit_coverage(self):
        rows = [dict(source="provider", event=event, provider="crm", method="search",
            provider_call_id="lookup", correlation_sha256="digest", monotonic_ns=at,
            lookup_status="absent", lookup_records=[]) for event, at in (("provider_start", 10), ("provider_end", 20))]
        raw = dict(ledger=rows + [dict(source="provider", event="effect", provider="crm",
            correlation_sha256="digest", monotonic_ns=40)], observation_finished_monotonic_ns=30,
            http=[dict(source="http_client", event=event, provider="crm", method="GET", path="/records",
                request_id="get", correlation_sha256="digest", monotonic_ns=at, status_code=200)
                for event, at in (("http_start", 5), ("http_response", 25))])
        self.assertNotIn("late_commit_after_empty_lookup", coverage.observed(raw))
        raw["observation_finished_monotonic_ns"] = 50
        self.assertIn("late_commit_after_empty_lookup", coverage.observed(raw))


class ScoringTests(unittest.TestCase):
    def test_rate_limit_safety_is_separate_from_one_of_eight_completions(self):
        row = result(expected=8)
        row["operations"] = [dict(row["operations"][0], index=i,
            outcome_class="confirmed_success" if i == 7 else "confirmed_failure",
            effects=int(i == 7), safe_completion=i == 7) for i in range(8)]
        summary = scoring.summarize([row])
        counts = summary["by_arm"]["safeact"]
        self.assertEqual(counts["safety_failure_rows"], 0)
        self.assertEqual(counts["usefulness"], dict(completed=1, achievable=8, fraction=0.125))
        self.assertEqual(counts["outcomes"]["confirmed_failure"], 7)
        self.assertIn("1/8", report.render(summary))

    def test_refusals_and_harness_errors_stay_in_achievable_denominator(self):
        for changes in (dict(operations=[]), dict(harness_errors=["setup failed"], operations=[]),
                        dict(cleanup_incomplete=True), dict(prohibited_duplicates=1)):
            with self.subTest(changes=changes):
                counts = scoring.aggregate([result(**changes)])
                self.assertEqual(counts["usefulness"]["achievable"], 1)
                self.assertEqual(counts["usefulness"]["completed"], 0)

    def test_permanent_failure_can_satisfy_its_declared_terminal(self):
        row = result(completed=False)
        row["operations"][0]["safe_completion"] = True
        self.assertEqual(scoring.aggregate([row])["usefulness"]["completed"], 1)

    def test_na_is_counted_separately_and_never_passed(self):
        counts = scoring.aggregate([result(not_applicable=True)])
        self.assertEqual(counts["not_applicable"], 1)
        self.assertEqual(counts["usefulness"], dict(completed=0, achievable=0, fraction=None))
        self.assertEqual(counts["applicable_operations"], 0)

    def test_missing_or_duplicate_denominators_fail_instead_of_hiding_cases(self):
        for row in (result(expected_operations=None), result(expected_operations=0),
                    result(operations=[result()["operations"][0]] * 2, expected=2)):
            with self.assertRaises(ValueError):
                scoring.aggregate([row])
        with self.assertRaisesRegex(ValueError, "Duplicate scenario"):
            scoring.summarize([result(), result()])

    def test_nonachievable_operations_are_not_useful_completions(self):
        counts = scoring.aggregate([result(achievable=False)])
        self.assertIsNone(counts["usefulness"]["fraction"])
        self.assertEqual(counts["outcomes"]["confirmed_success"], 1)

    def test_latency_distributions_expose_invalid_values(self):
        summary = scoring.distribution([-1, float("nan"), 1, 2, 3, True])
        self.assertEqual(summary, dict(count=3, invalid=3, p50=2, p95=3, maximum=3))


if __name__ == "__main__":
    unittest.main()
