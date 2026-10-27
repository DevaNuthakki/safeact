"""Actual hook errors, persisted transactions and supervised process deaths."""

import copy
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chaos.coverage import observed
from chaos.quiescence import kill_parked_worker, stop_processes
from chaos.scenarios import generate
from deploy.stack import failpoints
from deploy.stack.execution_trace import ERROR, ExecutionTrace, ObservedHook, from_environment, read_trace


def commit_run():
    return dict(admissions=[dict(index=0, local_id="local", changed=False)],
        database=[dict(event="db_audit_committed", source="database", operation_id="local", commit_xid="123")],
        execution=[dict(source="execution", pid=10, stage="outcome", commit_xid="123",
            event=event, point=point, monotonic_ns=at, error_type="ConnectionError")
            for event, point, at in (("commit_before", "before_outcome_commit", 10),
                ("commit_returned", "after_outcome_commit", 20), ("hook_error", "after_outcome_commit", 30))],
        observation_finished_monotonic_ns=100)


def kill_run(after=True):
    point = "after_provider_call" if after else "after_fence_commit"
    details = dict(point=point, worker_pid=9, parked_pid=10, process_group=9, signal=9)
    return dict(admissions=[dict(index=0, local_id="local", changed=False)],
        execution=[dict(source="execution", pid=10, event="hook_enter", point=point, monotonic_ns=40)],
        timeline=[dict(event="worker_kill_requested", monotonic_ns=50, **details),
                  dict(event="worker_killed", monotonic_ns=60, returncode=-9, **details)],
        http=[dict(source="http_client", provider="payments", method="POST", pid=10,
            request_id="one", correlation_sha256="key", event=event, monotonic_ns=at)
            for event, at in (("http_start", 10), ("http_response", 30))] if after else [],
        ledger=[dict(source="provider", event=event, provider="payments", method="refund",
            correlation_sha256="key", monotonic_ns=at)
            for event, at in (("provider_start", 12), ("call", 15), ("effect", 20))] if after else [],
        observation_finished_monotonic_ns=100)


class HookTests(unittest.TestCase):
    def test_disabled_trace_preserves_hook_identity(self):
        hook = object()
        with patch.dict(os.environ, {}, clear=True):
            self.assertIs(from_environment("safeact_demo", hook), hook)

    def test_real_raised_lost_ack_is_recorded_with_its_precommit_xid(self):
        rows, failure = [], ConnectionError("private exception message")
        connection = SimpleNamespace(execute=lambda query: SimpleNamespace(fetchone=lambda: ("123",)))
        def fault(point, connection):
            if point == "after_outcome_commit":
                raise failure
        hook = ObservedHook(lambda event, **details: rows.append(dict(event=event, **details)), fault)
        hook("before_outcome_commit", connection)
        with self.assertRaises(ConnectionError) as caught:
            hook("after_outcome_commit", connection)
        self.assertIs(caught.exception, failure)
        self.assertEqual([r["event"] for r in rows], ["hook_enter", "commit_before", "hook_enter", "commit_returned", "hook_error"])
        self.assertEqual(rows[-1]["commit_xid"], "123")
        self.assertNotIn("private exception message", json.dumps(rows))

    def test_failed_observer_preserves_hook_result_and_marks_harness(self):
        def broken(*args, **kwargs):
            raise OSError("trace disk unavailable")
        with patch("deploy.stack.execution_trace.os.write") as write:
            self.assertEqual(ObservedHook(broken, lambda *args: "answer")("after_provider_call", None), "answer")
        self.assertEqual(write.call_args.args, (2, ERROR))

    def test_unassigned_xid_cannot_be_invented(self):
        rows = []
        connection = SimpleNamespace(execute=lambda query: SimpleNamespace(fetchone=lambda: (None,)))
        hook = ObservedHook(lambda event, **details: rows.append(dict(event=event, **details)))
        hook("before_claim_commit", connection)
        hook("after_claim_commit", connection)
        self.assertIsNone(rows[-1]["commit_xid"])

    def test_query_failure_marks_the_harness_instead_of_fabricating_xid(self):
        class Connection:
            def execute(self, query):
                raise RuntimeError("query unavailable")
        with patch("deploy.stack.execution_trace.os.write") as write:
            ObservedHook(lambda *args, **kwargs: None)("before_claim_commit", Connection())
        self.assertEqual(write.call_args.args, (2, ERROR))

    def test_private_trace_roundtrip_and_corruption_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = ExecutionTrace(directory, "safeact_it_" + "a" * 32)
            writer("hook_enter", point="after_provider_call")
            rows = read_trace(directory, 0)
            self.assertEqual(len(rows), 1)
            path = next(Path(directory).glob("execution-*.jsonl"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with path.open("a") as stream:
                stream.write(path.read_text())
            with self.assertRaises(RuntimeError):
                read_trace(directory, 0)
            with self.assertRaises(RuntimeError):
                ExecutionTrace(directory, "safeact_demo")


class CoverageTests(unittest.TestCase):
    def test_unprotected_fence_uncertainty_declares_escalation_not_completion(self):
        for family, index in (("F05", 0), ("F11", 5)):
            case = generate("dev", family, index, 7)
            self.assertFalse(case.completion_achievable)
            self.assertEqual(case.operations[0].acceptable_terminal, ("escalated_unresolved",))
        for family, index in (("F05", 1), ("F11", 2)):
            case = generate("dev", family, index, 7)
            self.assertTrue(case.completion_achievable)
            self.assertEqual(case.operations[0].acceptable_terminal, ("confirmed_success",))

    def test_commit_error_is_joined_to_independent_committed_audit_xid(self):
        self.assertIn("commit_ack_lost", observed(commit_run()))

    def test_configured_fault_or_committed_audit_alone_is_not_lost_ack(self):
        for key in ("execution", "database"):
            raw = commit_run()
            raw[key] = []
            self.assertNotIn("commit_ack_lost", observed(raw))

    def test_wrong_xid_pid_operation_or_exception_is_not_lost_ack(self):
        variants = []
        for key, value in (("commit_xid", "other"), ("pid", 11), ("error_type", "OSError")):
            raw = commit_run()
            raw["execution"][-1][key] = value
            variants.append(raw)
        raw = commit_run()
        raw["database"][0]["operation_id"] = "other"
        variants.append(raw)
        for raw in variants:
            self.assertNotIn("commit_ack_lost", observed(raw))

    def test_commit_or_error_order_outside_the_window_is_not_credited(self):
        for at in (5, 200):
            raw = commit_run()
            raw["execution"][-1]["monotonic_ns"] = at
            self.assertNotIn("commit_ack_lost", observed(raw))

    def test_observed_sigkill_after_effect_is_credited(self):
        self.assertIn("worker_crashed_after_send", observed(kill_run()))

    def test_observed_sigkill_before_send_is_distinct(self):
        self.assertIn("worker_crashed_before_send", observed(kill_run(False)))
        self.assertNotIn("worker_crashed_after_send", observed(kill_run(False)))

    def test_declared_kill_without_exit_or_hook_or_effect_is_not_credit(self):
        for key in ("execution", "ledger"):
            raw = kill_run()
            raw[key] = []
            self.assertNotIn("worker_crashed_after_send", observed(raw))
        for key, value in (("returncode", 0), ("signal", 15), ("parked_pid", 11)):
            raw = kill_run()
            raw["timeline"][-1][key] = value
            self.assertNotIn("worker_crashed_after_send", observed(raw))

    def test_prior_send_prevents_before_send_credit(self):
        raw = kill_run(False)
        raw["http"] = copy.deepcopy(kill_run()["http"])
        self.assertNotIn("worker_crashed_before_send", observed(raw))


class SupervisedKillTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "Process groups require POSIX")
    def test_real_sigkill_and_replacement_keep_all_generations_for_cleanup(self):
        class Stack:
            def __init__(self):
                self.processes = {}
            def spawn(self, component):
                process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
                self.processes[component] = process
                return process
            def kill(self, component, sig):
                process = self.processes[component]
                os.killpg(process.pid, sig)
                process.wait(5)
            def stop(self):
                pass
        stack, events = Stack(), []
        original = stack.spawn("worker")
        def cleanup():
            try:
                stop_processes(stack)
            finally:
                for process in stack.processes.values():
                    process.kill()
                for process in stack.processes.values():
                    process.wait(5)
        self.addCleanup(cleanup)
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "parked").write_text(f"after_provider_call {original.pid}\n")
            kill_parked_worker(stack, directory, "after_provider_call",
                lambda event, **details: events.append(dict(event=event, **details)))
        self.assertEqual(original.returncode, -signal.SIGKILL)
        self.assertNotEqual(stack.processes["worker"].pid, original.pid)
        self.assertIs(stack.processes[f"killed-worker-{original.pid}"], original)
        self.assertEqual(events[1]["returncode"], -signal.SIGKILL)

    def test_atomic_arming_refuses_paths_and_keeps_complete_action(self):
        with tempfile.TemporaryDirectory() as directory:
            failpoints.arm(directory, "after_claim_commit", "lost_ack")
            self.assertEqual(Path(directory, "after_claim_commit").read_text(), "lost_ack")
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
            with self.assertRaises(ValueError):
                failpoints.arm(directory, "../outside", "crash")


if __name__ == "__main__":
    unittest.main()
