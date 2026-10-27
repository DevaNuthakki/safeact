"""Close provider admission and prove process groups have stopped (harness only)."""

import os
import signal
import subprocess
import threading
import time

from contextlib import contextmanager
from pathlib import Path


class ProviderBarrier:
    """No call can enter after close; idle means all entered calls returned."""

    def __init__(self):
        self._condition = threading.Condition()
        self._closed = False
        self._active = 0

    @contextmanager
    def request(self):
        with self._condition:
            if self._closed:
                raise RuntimeError("Provider admission is closed")
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True

    def wait_idle(self, timeout):
        with self._condition:
            if not self._closed:
                raise RuntimeError("Close provider admission before waiting")
            if not self._condition.wait_for(lambda: self._active == 0, timeout):
                raise RuntimeError("Provider calls did not finish before the drain deadline")


def kill_parked_worker(stack, directory, point, emit):
    reached, child = (Path(directory) / "parked").read_text().split()
    child = int(child)
    process = stack.processes["worker"]
    if reached != point or process.poll() is not None or os.getpgid(child) != process.pid:
        raise RuntimeError("Parked process is not in this worker's live process group")
    details = dict(point=point, worker_pid=process.pid, parked_pid=child,
                   process_group=process.pid, signal=int(signal.SIGKILL))
    emit("worker_kill_requested", **details)
    stack.kill("worker", signal.SIGKILL)
    if process.returncode != -signal.SIGKILL:
        raise RuntimeError("Worker exit was not SIGKILL")
    stack.processes[f"killed-worker-{process.pid}"] = process
    emit("worker_killed", **details, returncode=process.returncode)
    replacement = stack.spawn("worker")
    emit("worker_restarted", worker_pid=replacement.pid, previous_pid=process.pid)


def process_group_exists(pgid):
    """Resolve an ambiguous EPERM using a checked, numeric-only process table."""
    if type(pgid) is not int or pgid <= 1:
        raise ValueError("Expected a private process group")
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin excludes zombies from signalable members. EPERM alone proves
        # neither presence nor absence, so require an independent enumeration.
        try:
            result = subprocess.run(
                ["/bin/ps", "-A", "-o", "pid=", "-o", "pgid="],
                env={**os.environ, "LC_ALL": "C"}, check=True,
                capture_output=True, text=True, timeout=1,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError("Process table could not be read") from error
        if result.stderr.strip():
            raise RuntimeError("Process table reported diagnostics")
        groups = {}
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 2 or any(not value.isascii() or not value.isdigit() for value in fields):
                raise RuntimeError("Invalid process table")
            pid, group = map(int, fields)
            if pid in groups:
                raise RuntimeError("Duplicate process table entry")
            groups[pid] = group
        if groups.get(os.getpid()) != os.getpgrp():
            raise RuntimeError("Incomplete process table")
        # Retain even zombie members until they disappear from the table.
        return pgid in groups.values()
    return True


def stop_processes(stack, timeout=5):
    """Stop the stack, including children whose original parent already exited."""
    processes = list(stack.processes.values())
    if any(type(p.pid) is not int or p.pid <= 1 or p.pid == os.getpgrp() for p in processes):
        raise RuntimeError("Refusing to signal an unsafe process group")
    problem = None
    try:
        stack.stop()
    except Exception as error:
        problem = error
    signal_errors = {}
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as error:
            # An error on one group must not prevent killing the others.
            signal_errors[process.pid] = type(error).__name__
    deadline = time.monotonic() + timeout
    pending = processes[:]
    while pending:
        # Reap every owned child before any probe can fail.
        statuses = [(process, process.poll()) for process in pending]
        live, probe_errors = [], {}
        for process, returncode in statuses:
            try:
                exists = process_group_exists(process.pid)
            except (OSError, RuntimeError) as error:
                exists = True
                probe_errors[process.pid] = f"{type(error).__name__}: {error}"
            if exists or returncode is None:
                live.append(process)
        pending = live
        if not pending:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Process groups still exist or cannot be verified after shutdown: {[p.pid for p in pending]}; "
                f"signal_errors={signal_errors}; probe_errors={probe_errors}"
            )
        time.sleep(0.02)
    if problem is not None:
        raise RuntimeError("Stack shutdown reported an error") from problem
    return [{"pid": process.pid, "returncode": process.poll()}
            for process in processes]
