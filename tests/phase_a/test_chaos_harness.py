"""Counterexamples for the development qualification harness (no services)."""

import io
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chaos import coverage, oracle, qualify
from chaos.arms.base import Admission
from chaos.runner import FaultDriver, SubmissionError, Timeline, _ledger_for, _submit_all, run
from chaos.scenarios import OperationSpec, Scenario, generate
from connectors.ledger import EffectsLedger


def scenario(**changes):
    values = dict(scenario_id="D-CONTROL-0000", split="dev", family="CONTROL",
                  connector="payments", contract_version="q-v2", seed=7,
                  operations=(OperationSpec("ch_expected", 100),), faults=(),
                  completion_achievable=True, settle_seconds=1)
    values.update(changes)
    return Scenario(**values)


def raw_for(case, *, effects=1, outcome="confirmed_success"):
    resource = case.operations[0].resource_id
    return {"scenario_id": case.scenario_id, "arm": "safeact", "started": 100,
            "finished": 110, "admissions": [dict(index=0, copy=0, changed=False,
             local_id="local-1", refused=None)],
            "operations": {"local-1": dict(outcome_class=outcome, state="DONE",
             resolved_at=109, escalated_at=None, events=[], evidence_rows=1)},
            "timeline": [dict(event="submission_started", index=0, at=100,
                              changed=False)],
            "ledger": [dict(event="call", resource=resource, at=102)] +
                      [dict(event="effect", resource=resource, at=103)
                       for _ in range(effects)], "integrity": []}


class OracleTests(unittest.TestCase):
    def test_duplicate_effect_blocks_safeact_exit_and_usefulness(self):
        case = scenario()
        result = oracle.evaluate(case, raw_for(case, effects=2))
        self.assertEqual(result["prohibited_duplicates"], 1)
        self.assertFalse(result["operations"][0]["safe_completion"])
        self.assertEqual(qualify.exit_code([result], 1), 1)
        result["arm"] = "b0"
        self.assertEqual(qualify.exit_code([result], 1), 0)

    def per_intent(self, *, effects, copies=2):
        """One intended action (a resource) admitted as several operations."""
        case = scenario(operations=(OperationSpec("ch_expected", 100, submits=copies),))
        raw = raw_for(case, effects=effects)
        raw["admissions"] = [dict(index=0, copy=n, changed=False, local_id=f"local-{n}",
                                  refused=None) for n in range(copies)]
        raw["operations"] = {f"local-{n}": raw["operations"]["local-1"] for n in range(copies)}
        return oracle.evaluate(case, raw)

    def test_two_operations_for_one_intent_count_as_a_prohibited_duplicate(self):
        # Each operation applied once; per intent that is a second effect.
        result = self.per_intent(effects=2)
        self.assertEqual(result["operations"][0]["accepted_operations"], 2)
        self.assertEqual(result["prohibited_duplicates"], 1)
        self.assertTrue(oracle.safety_failures(result))
        self.assertEqual(self.per_intent(effects=3, copies=3)["prohibited_duplicates"], 2)

    def test_split_intent_with_one_effect_is_visible_but_not_a_duplicate(self):
        result = self.per_intent(effects=1)
        self.assertEqual(result["operations"][0]["accepted_operations"], 2)
        self.assertEqual(result["prohibited_duplicates"], 0)

    def test_distinct_intents_with_one_effect_each_are_not_duplicates(self):
        case = scenario(operations=(OperationSpec("ch_a", 100), OperationSpec("ch_b", 200)))
        raw = raw_for(case)
        raw["admissions"].append(dict(index=1, copy=0, changed=False, local_id="local-2", refused=None))
        raw["operations"]["local-2"] = raw["operations"]["local-1"]
        raw["timeline"].append(dict(event="submission_started", index=1, at=100, changed=False))
        raw["ledger"] += [dict(event="call", resource="ch_b", at=102), dict(event="effect", resource="ch_b", at=103)]
        result = oracle.evaluate(case, raw)
        self.assertEqual([o["effects"] for o in result["operations"]], [1, 1])
        self.assertEqual(result["prohibited_duplicates"], 0)

    def test_unexpected_and_unattributed_effects_survive_ledger_slicing(self):
        ledger = EffectsLedger()
        ledger.record_call("warmup", {"charge_id": "old"})
        ledger.record_effect("warmup", "old-effect")
        offset = len(ledger.timeline())
        ledger.record_call("unexpected", {"charge_id": "ch_wrong"})
        ledger.record_effect("unexpected", "wrong-effect")
        ledger.record_effect("no-call", "unattributed-effect")
        events = _ledger_for(SimpleNamespace(ledger=ledger), {"ch_expected"},
                             time.time(), offset=offset)
        self.assertEqual(len(events), 3)
        case = scenario()
        raw = raw_for(case)
        raw["ledger"].extend(events)
        result = oracle.evaluate(case, raw)
        self.assertEqual(result["orphan_effects"], 2)
        self.assertFalse(result["operations"][0]["safe_completion"])

    def test_conflicting_resource_mapping_is_not_silently_reassigned(self):
        ledger = EffectsLedger()
        ledger.record_call("same-key", {"charge_id": "ch_expected"})
        ledger.record_effect("same-key", "first")
        ledger.record_call("same-key", {"charge_id": "ch_wrong"})
        events = _ledger_for(SimpleNamespace(ledger=ledger), {"ch_expected"}, 0)
        self.assertIsNone(next(e for e in events if e["event"] == "effect")["resource"])

    def test_every_accepted_local_id_must_be_accounted_for(self):
        case = scenario(operations=(OperationSpec("ch_expected", 100, submits=2),))
        raw = raw_for(case)
        raw["admissions"].append(dict(index=0, copy=1, changed=False,
                                      local_id="lost-local", refused=None))
        result = oracle.evaluate(case, raw)
        self.assertEqual(result["unaccounted"], 1)
        self.assertFalse(result["operations"][0]["safe_completion"])

    def test_later_local_success_claim_is_checked(self):
        case = scenario(operations=(OperationSpec("ch_expected", 100, submits=2),))
        raw = raw_for(case, effects=0, outcome="confirmed_failure")
        raw["admissions"].append(dict(index=0, copy=1, changed=False,
                                      local_id="second", refused=None))
        raw["operations"]["second"] = dict(raw["operations"]["local-1"],
                                           outcome_class="confirmed_success")
        self.assertEqual(oracle.evaluate(case, raw)["false_claims"], 1)

    def test_failing_a_control_is_not_useful_completion(self):
        case = scenario()
        result = oracle.evaluate(case, raw_for(case, effects=0, outcome="confirmed_failure"))
        self.assertFalse(result["operations"][0]["safe_completion"])
        permanent = scenario(operations=(OperationSpec("ch_expected", 100,
                             acceptable_terminal=("confirmed_failure",)),))
        result = oracle.evaluate(permanent, raw_for(permanent, effects=0,
                                                   outcome="confirmed_failure"))
        self.assertTrue(result["operations"][0]["safe_completion"])

    def test_valid_completion_and_measured_queue_wait(self):
        case = scenario()
        result = oracle.evaluate(case, raw_for(case))
        self.assertTrue(result["operations"][0]["safe_completion"])
        self.assertEqual(result["operations"][0]["queue_wait_seconds"], 2)
        self.assertEqual(qualify.exit_code([result], 1), 0)
        self.assertEqual(qualify.exit_code([], 1), 1)
        self.assertEqual(qualify.exit_code([result, result], 2), 1)


class DriverTests(unittest.TestCase):
    def test_failed_run_preserves_trace_and_cleans_up(self):
        class Arm:
            name = "safeact"
            def prepare(self, case):
                pass
            def submit(self, *args, **kwargs):
                raise ConnectionError("admission failed")
            def check_integrity(self):
                return []
            def quiesce(self):
                pass
            def observe(self, ids):
                return {}
        with tempfile.TemporaryDirectory() as directory:
            slot = SimpleNamespace(index=0, ledger=EffectsLedger(),
                                   fault_dir=Path(directory), reset=lambda: None,
                                   close_provider_admission=lambda: None,
                                   wait_provider_idle=lambda timeout: None)
            raw = run(slot, Arm(), scenario())
        self.assertIn("admission failed", raw["harness_errors"][0])
        self.assertTrue(any(e["event"] == "submission_error" for e in raw["timeline"]))
        self.assertFalse(raw["cleanup_incomplete"])

    def test_concurrent_submission_exception_reaches_harness(self):
        class Arm:
            def submit(self, *args, **kwargs):
                raise ConnectionError("admission database unavailable")
        case = scenario(operations=(OperationSpec("ch_expected", 100, submits=3,
                                                  concurrent=True),))
        timeline = Timeline()
        with self.assertRaises(SubmissionError) as error:
            _submit_all(Arm(), case, timeline)
        self.assertEqual(error.exception.admissions, [])
        self.assertEqual(sum(e["event"] == "submission_error" for e in timeline.events), 3)
        self.assertEqual([e["sequence"] for e in timeline.events], list(range(6)))

    def test_fault_thread_error_is_not_discarded(self):
        driver = FaultDriver(None, Timeline())
        def broken():
            raise ConnectionError("toxiproxy unavailable")
        driver.later(0, broken, "toxic_on")
        driver.timers[0].join(1)
        driver.cancel()
        with self.assertRaisesRegex(RuntimeError, "toxiproxy unavailable"):
            driver.check()

    def test_cancel_interrupts_triggered_fault_delay(self):
        driver = FaultDriver(None, Timeline())
        called, ready = threading.Event(), threading.Event()
        def count_calls():
            ready.set()
            return 1
        driver.calls = count_calls
        driver.when({"after_call": 1}, 60000, called.set, "late_fault")
        self.assertTrue(ready.wait(1))
        driver.cancel()
        self.assertFalse(driver.pending())
        self.assertFalse(called.is_set())

    def test_switches_states_and_effects_do_not_prove_fault_coverage(self):
        case = scenario()
        raw = raw_for(case)
        raw["timeline"] += [dict(event="fault_set", switch=switch, at=101)
                            for switch in ("fail_after_effect", "server_error_after_effect",
                                           "lookup_unavailable", "corrupt_search")]
        raw["timeline"] += [dict(event=label, at=101) for label in (
            "hold_released", "redis_flushed", "rate_limit_set", "outage_on", "revealed")]
        raw["operations"]["local-1"]["events"] = [(101, "VERIFYING:lookup"),
            (102, "ESCALATED_UNRESOLVED:deadline")]
        self.assertEqual(coverage.observed(raw), set())

    def test_admission_evidence_can_prove_collapse_and_changed_refusal(self):
        raw = {"admissions": [dict(index=0, local_id="same", changed=False, refused=None),
                              dict(index=0, local_id="same", changed=False, refused=None),
                              dict(index=0, local_id=None, changed=True, refused="conflict")]}
        self.assertEqual(coverage.observed(raw), {
            "duplicate_submissions_collapsed", "changed_payload_refused"})
        raw["admissions"] = [raw["admissions"][-1]]
        self.assertEqual(coverage.observed(raw), set())

    @unittest.skipUnless(os.name == "posix", "SIGKILL test requires POSIX")
    def test_crash_failpoint_really_sends_sigkill(self):
        with tempfile.TemporaryDirectory() as directory:
            code = ("import sys; from deploy.stack.failpoints import arm, FailpointTable; "
                    "arm(sys.argv[1], 'after_provider_call', 'crash'); "
                    "FailpointTable(sys.argv[1], 'safeact_it_' + 'a'*32)"
                    "('after_provider_call', None)")
            child = subprocess.run([sys.executable, "-c", code, directory],
                                   capture_output=True, timeout=5)
            self.assertEqual(child.returncode, -signal.SIGKILL, child.stderr.decode())
            self.assertIn("after_provider_call crash", (Path(directory) / "fired").read_text())


class QualificationTests(unittest.TestCase):
    def test_cli_fails_on_duplicate_and_keeps_evidence(self):
        class Slot:
            def start(self):
                pass
            def stop(self):
                pass
        class Arm:
            name, not_applicable = "safeact", ()
            def setup(self, slot, workdir):
                pass
            def teardown(self):
                pass
        api = SimpleNamespace(logging=SimpleNamespace(configure=lambda: None))
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"SAFEACT_RUN_QUALIFICATION": "1"}), \
                patch.dict(sys.modules, {"api": api}), \
                patch.object(qualify.tempfile, "mkdtemp", return_value=str(Path(directory) / "work")), \
                patch.object(qualify.environment, "describe", return_value={}), \
                patch.object(qualify, "arms_named", return_value=[Arm]), \
                patch.object(qualify, "make_slot", return_value=Slot()), \
                patch.object(qualify, "run", side_effect=lambda slot, arm, case:
                             raw_for(case, effects=2)):
            output = Path(directory) / "output"
            self.assertEqual(qualify.main(["--families", "CONTROL", "--limit", "1",
                                          "--out", str(output)]), 1)
            row = json.loads((output / "results.jsonl").read_text())
            self.assertEqual(row["prohibited_duplicates"], 1)
            self.assertTrue((output / "raw.jsonl").is_file())
            self.assertFalse(json.loads((output / "coverage.json").read_text())["complete"])
            counts = json.loads((output / "summary.json").read_text())["by_arm"]["safeact"]
            self.assertEqual(counts["usefulness"]["completed"], 0)
            self.assertEqual(counts["usefulness"]["achievable"], 1)
            self.assertIn("0/1", (output / "report.md").read_text())

    def test_empty_unsupported_and_non_development_selection_is_refused(self):
        for changes in ({"limit": 0}, {"families": "F99"},
                        {"families": "F99"}, {"corpus": "qual_v1"}):
            values = dict(corpus="dev", families=None, limit=1)
            values.update(changes)
            with self.assertRaises(SystemExit):
                qualify.scenarios_for(SimpleNamespace(**values))
        with self.assertRaises(ValueError):
            generate("dev", "F06+F07", 0, 7)

    def test_generated_scenario_roundtrip_preserves_expected_terminal(self):
        for index in range(20):
            case = generate("dev", "F02", index, 7)
            self.assertEqual(Scenario.from_json(case.to_json()), case)
            if "permanent" in case.notes:
                self.assertEqual(case.operations[0].acceptable_terminal,
                                 ("confirmed_failure",))

    def test_each_case_gets_fresh_slot_and_arm_and_setup_failure_is_saved(self):
        events = []
        class Slot:
            def start(self):
                events.append(("slot_start", id(self)))
            def stop(self):
                events.append(("slot_stop", id(self)))
        class Arm:
            name, not_applicable = "safeact", ()
            def setup(self, slot, workdir):
                self.slot = slot
                events.append(("arm_setup", id(self)))
                if len([e for e in events if e[0] == "arm_setup"]) == 2:
                    raise RuntimeError("partial setup failure")
            def teardown(self):
                events.append(("arm_stop", id(self)))
        slots = [Slot(), Slot()]
        work = queue.Queue()
        for index in range(2):
            work.put(scenario(scenario_id=f"D-CONTROL-{index:04d}"))
        out, errors = {"raw": io.StringIO(), "results": io.StringIO()}, []
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(qualify, "make_slot", side_effect=slots), \
                patch.object(qualify, "run", side_effect=lambda slot, arm, case: raw_for(case)):
            qualify.slot_worker(0, [Arm], work, out, threading.Lock(), Path(directory), errors)
        rows = [json.loads(line) for line in out["results"].getvalue().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({identity for name, identity in events if name == "slot_start"}), 2)
        self.assertEqual(sum(name == "arm_stop" for name, _ in events), 2)
        self.assertIn("partial setup failure", rows[1]["harness_errors"][0])
        self.assertEqual(qualify.exit_code(rows, 2), 1)


if __name__ == "__main__":
    unittest.main()
