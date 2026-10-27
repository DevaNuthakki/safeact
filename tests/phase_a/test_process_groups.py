"""Ambiguous group probes must not hide survivors or interrupt other cleanup."""

import os
import signal
import subprocess
import sys
import unittest

from types import SimpleNamespace
from unittest.mock import Mock, patch

from chaos.quiescence import process_group_exists, stop_processes


def table(extra="", *, stderr=""):
    return SimpleNamespace(stdout=f"{os.getpid()} {os.getpgrp()}\n" + extra, stderr=stderr)


def stack_for(*pids):
    return SimpleNamespace(processes={str(pid): SimpleNamespace(pid=pid, poll=Mock(return_value=0))
        for pid in pids}, stop=Mock())


class ProcessGroupTests(unittest.TestCase):
    def test_invalid_group_cannot_be_probed(self):
        for pgid in (0, 1, -1, True, "12345"):
            with self.subTest(pgid=pgid), patch("chaos.quiescence.os.killpg") as kill, self.assertRaises(ValueError):
                process_group_exists(pgid)
            kill.assert_not_called()

    def test_own_or_nonprivate_group_cannot_be_signalled(self):
        for pgid in (0, 1, os.getpgrp()):
            stack = stack_for(pgid)
            with self.subTest(pgid=pgid), patch("chaos.quiescence.os.killpg") as kill, \
                    self.assertRaisesRegex(RuntimeError, "unsafe process group"):
                stop_processes(stack)
            stack.stop.assert_not_called()
            kill.assert_not_called()

    def test_esrch_proves_absence_without_process_table(self):
        with patch("chaos.quiescence.os.killpg", side_effect=ProcessLookupError()), \
                patch("chaos.quiescence.subprocess.run") as ps:
            self.assertFalse(process_group_exists(12345))
        ps.assert_not_called()

    def test_successful_probe_is_live_without_process_table(self):
        with patch("chaos.quiescence.os.killpg"), patch("chaos.quiescence.subprocess.run") as ps:
            self.assertTrue(process_group_exists(12345))
        ps.assert_not_called()

    def test_eperm_requires_checked_enumeration_before_absence(self):
        with patch("chaos.quiescence.os.killpg", side_effect=PermissionError()), \
                patch("chaos.quiescence.subprocess.run", return_value=table()) as ps:
            self.assertFalse(process_group_exists(12345))
        self.assertEqual(ps.call_args.args[0], ["/bin/ps", "-A", "-o", "pid=", "-o", "pgid="])
        self.assertTrue(ps.call_args.kwargs["check"])
        self.assertEqual(ps.call_args.kwargs["timeout"], 1)

    def test_eperm_cannot_hide_group_member_when_leader_exited(self):
        with patch("chaos.quiescence.os.killpg", side_effect=PermissionError()), \
                patch("chaos.quiescence.subprocess.run", return_value=table("12346 12345\n")):
            self.assertTrue(process_group_exists(12345))

    def test_invalid_or_incomplete_process_table_never_proves_absence(self):
        for answer in (table("bad row\n"), table("123 456 extra\n"), table("-1 12345\n"),
                table(f"{os.getpid()} {os.getpgrp()}\n"), table(stderr="partial result"),
                SimpleNamespace(stdout="", stderr="")):
            with self.subTest(answer=answer), patch("chaos.quiescence.os.killpg", side_effect=PermissionError()), \
                    patch("chaos.quiescence.subprocess.run", return_value=answer), self.assertRaises(RuntimeError):
                process_group_exists(12345)

    def test_process_table_failure_never_proves_absence(self):
        for error in (OSError(), subprocess.CalledProcessError(1, ["ps"]), subprocess.TimeoutExpired(["ps"], 1)):
            with self.subTest(error=type(error).__name__), patch("chaos.quiescence.os.killpg", side_effect=PermissionError()), \
                    patch("chaos.quiescence.subprocess.run", side_effect=error), self.assertRaisesRegex(RuntimeError, "could not be read"):
                process_group_exists(12345)

    def test_signal_denial_does_not_interrupt_other_group_cleanup(self):
        stack = stack_for(12345, 12346)
        with patch("chaos.quiescence.os.killpg", side_effect=[PermissionError(), None]) as kill, \
                patch("chaos.quiescence.process_group_exists", side_effect=[True, False]), \
                self.assertRaisesRegex(RuntimeError, "still exist"):
            stop_processes(stack, timeout=0)
        self.assertEqual([call.args for call in kill.call_args_list],
            [(12345, signal.SIGKILL), (12346, signal.SIGKILL)])
        for process in stack.processes.values():
            process.poll.assert_called_once()

    def test_failed_probe_does_not_skip_reaping_or_checking_other_group(self):
        stack = stack_for(12345, 12346)
        with patch("chaos.quiescence.os.killpg"), \
                patch("chaos.quiescence.process_group_exists", side_effect=[RuntimeError("unavailable"), False]) as probe, \
                self.assertRaisesRegex(RuntimeError, "cannot be verified"):
            stop_processes(stack, timeout=0)
        self.assertEqual(probe.call_count, 2)
        for process in stack.processes.values():
            process.poll.assert_called_once()

    def test_missing_group_does_not_excuse_unreaped_owned_child(self):
        stack = stack_for(12345)
        stack.processes["12345"].poll.return_value = None
        with patch("chaos.quiescence.os.killpg"), \
                patch("chaos.quiescence.process_group_exists", return_value=False), \
                self.assertRaisesRegex(RuntimeError, "cannot be verified"):
            stop_processes(stack, timeout=0)

    def test_stack_stop_error_is_retained_after_verified_cleanup(self):
        stack = stack_for(12345)
        failure = RuntimeError("stop failed")
        stack.stop.side_effect = failure
        with patch("chaos.quiescence.os.killpg") as kill, \
                patch("chaos.quiescence.process_group_exists", return_value=False), \
                self.assertRaisesRegex(RuntimeError, "Stack shutdown") as caught:
            stop_processes(stack, timeout=0)
        self.assertIs(caught.exception.__cause__, failure)
        kill.assert_called_once_with(12345, signal.SIGKILL)

    @unittest.skipUnless(os.name == "posix", "Process groups require POSIX")
    def test_real_child_is_reaped_even_when_group_probe_is_denied(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.addCleanup(child.wait, 5)
        self.addCleanup(child.kill)
        with patch("chaos.quiescence.process_group_exists", side_effect=PermissionError()), \
                self.assertRaisesRegex(RuntimeError, "cannot be verified"):
            stop_processes(SimpleNamespace(processes={"worker": child}, stop=lambda: None), timeout=0.2)
        self.assertEqual(child.poll(), -signal.SIGKILL)
