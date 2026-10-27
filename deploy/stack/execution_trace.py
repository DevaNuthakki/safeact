"""Disposable-only hook observations; commit visibility is checked by the harness.

Before a commit, SELECT the transaction id already assigned to that connection.
After commit, record its return and any exception actually raised by the fault
hook. No credentials, SQL parameters or exception messages are recorded.
"""

import fcntl
import json
import os
import re
import stat
import time

from pathlib import Path

from deploy.stack.http_trace import HttpTrace


ERROR = b"SAFEACT_EXEC_TRACE_ERROR\n"
EVENTS = frozenset({"hook_enter", "commit_before", "commit_returned", "hook_error"})


class ExecutionTrace(HttpTrace):
    def __call__(self, event, **details):
        if event not in EVENTS:
            raise ValueError("Unknown execution observation")
        row = dict(details, event=event, source="execution", pid=os.getpid(),
                   at=time.time(), monotonic_ns=time.monotonic_ns())
        data = (json.dumps(row, sort_keys=True) + "\n").encode()
        if len(data) > 4096:
            raise ValueError("Execution observation exceeds its size limit")
        fd = os.open(self.directory / f"execution-{os.getpid()}.jsonl",
            os.O_APPEND | os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise RuntimeError("Execution trace must be a private regular file")
            fcntl.flock(fd, fcntl.LOCK_EX)
            if os.write(fd, data) != len(data):
                raise OSError("Incomplete execution trace write")
        finally:
            os.close(fd)


def _failed():
    try:
        os.write(2, ERROR)
    except OSError:
        pass


class ObservedHook:
    def __init__(self, writer, hook=None):
        self.writer = writer
        self.hook = hook or (lambda point, connection: None)
        self.pending = {}

    def emit(self, event, **details):
        try:
            self.writer(event, **details)
        except Exception:
            _failed()

    def __call__(self, point, connection):
        self.emit("hook_enter", point=point)
        stage = point.split("_", 1)[1][:-7] if point.endswith("_commit") else None
        xid = None
        if stage is not None and point.startswith("before_"):
            try:
                xid = connection.execute(
                    "SELECT pg_current_xact_id_if_assigned()::text").fetchone()[0]
            except Exception:
                _failed()
            self.pending[stage] = xid
            self.emit("commit_before", point=point, stage=stage, commit_xid=xid)
        elif stage is not None and point.startswith("after_"):
            xid = self.pending.pop(stage, None)
            self.emit("commit_returned", point=point, stage=stage, commit_xid=xid)
        try:
            return self.hook(point, connection)
        except Exception as error:
            self.emit("hook_error", point=point, stage=stage, commit_xid=xid,
                      error_type=type(error).__name__)
            raise


def from_environment(schema, hook=None):
    directory = os.environ.get("SAFEACT_STACK_EXEC_TRACE_DIR")
    return ObservedHook(ExecutionTrace(directory, schema), hook) if directory else hook


def read_trace(directory, since_ns):
    rows, seen = [], set()
    for path in sorted(Path(directory).glob("execution-*.jsonl")):
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            for number, line in enumerate(stream, 1):
                try:
                    row = json.loads(line)
                    if (row["event"] not in EVENTS or row["source"] != "execution"
                            or type(row["monotonic_ns"]) is not int or type(row["pid"]) is not int
                            or not re.fullmatch(r"[a-z_]+", row["point"])
                            or (row.get("commit_xid") is not None
                                and not re.fullmatch(r"[0-9]{1,20}", row["commit_xid"]))):
                        raise ValueError("Invalid execution observation")
                    identity = (row["pid"], row["monotonic_ns"], row["event"], row["point"])
                    if identity in seen:
                        raise ValueError("Duplicate execution observation")
                    seen.add(identity)
                except (KeyError, TypeError, ValueError) as error:
                    raise RuntimeError(f"Invalid execution trace at {path.name}:{number}") from error
                if row["monotonic_ns"] >= since_ns:
                    rows.append(row)
    return sorted(rows, key=lambda row: row["monotonic_ns"])
