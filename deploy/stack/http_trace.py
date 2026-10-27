"""Private, append-only HTTP trace files for disposable qualification schemas."""

import fcntl
import json
import os
import re
import stat
import time

from pathlib import Path


DISPOSABLE = re.compile(r"safeact_it_[0-9a-f]{32}")
EVENTS = frozenset({"http_start", "http_response", "http_error"})


class HttpTrace:
    def __init__(self, directory, schema):
        if not DISPOSABLE.fullmatch(schema):
            raise RuntimeError("HTTP tracing requires a disposable test schema")
        self.directory = Path(directory)
        info = self.directory.stat()
        if (self.directory.is_symlink() or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise RuntimeError("HTTP trace directory must be private and owned by this user")

    def __call__(self, event, **details):
        if event not in EVENTS:
            raise ValueError("Unknown HTTP observation")
        row = dict(details, event=event, source="http_client", pid=os.getpid(),
                   at=time.time(), monotonic_ns=time.monotonic_ns())
        data = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
        if len(data) > 4096:
            raise ValueError("HTTP observation exceeds its size limit")
        path = self.directory / f"http-{os.getpid()}.jsonl"
        fd = os.open(path, os.O_APPEND | os.O_WRONLY | os.O_CREAT |
                     getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise RuntimeError("HTTP trace must be a private regular file")
            fcntl.flock(fd, fcntl.LOCK_EX)
            if os.write(fd, data) != len(data):
                raise OSError("Incomplete HTTP trace write")
        finally:
            os.close(fd)


def from_environment(schema):
    directory = os.environ.get("SAFEACT_STACK_HTTP_TRACE_DIR")
    return HttpTrace(directory, schema) if directory else None


def read_trace(directory, since_ns):
    rows, seen = [], set()
    for path in sorted(Path(directory).glob("http-*.jsonl")):
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            for number, line in enumerate(stream, 1):
                try:
                    row = json.loads(line)
                    if (row["event"] not in EVENTS or row["source"] != "http_client"
                            or not isinstance(row["monotonic_ns"], int)
                            or not isinstance(row["at"], (int, float))
                            or not re.fullmatch(r"[0-9a-f]{32}", row["request_id"])
                            or not re.fullmatch(r"[0-9a-f]{64}", row["correlation_sha256"])):
                        raise ValueError("Invalid observation")
                except (ValueError, KeyError, TypeError) as error:
                    raise RuntimeError(f"Invalid HTTP trace at {path.name}:{number}") from error
                identity = (row["request_id"], "start" if row["event"] == "http_start" else "end")
                if identity in seen:
                    raise RuntimeError(f"Duplicate HTTP observation at {path.name}:{number}")
                seen.add(identity)
                if row["monotonic_ns"] >= since_ns:
                    rows.append(row)
    return sorted(rows, key=lambda row: row["monotonic_ns"])
