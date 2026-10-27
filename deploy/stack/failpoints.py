"""Harness-only deterministic failpoints for the stack's processes (M5.9).

Network faults (Toxiproxy) cannot choose a commit or acknowledgement
boundary; these can. The qualification harness arms a failpoint by
writing a file named after an ExecutionWorker or Reconciler fault point
(for example ``after_outcome_commit``) into SAFEACT_STACK_FAULT_DIR. The
first process to reach that point takes the file with an atomic rename,
so it fires exactly once across all processes, and acts on its content:

  lost_ack   raise ConnectionError after the commit: the commit landed but
             the caller never learns it (F11)
  crash      send SIGKILL to this process (F05)
  park       write "<point> <pid>" to <dir>/parked and wait to be killed
  sleep:<s>  wait <s> seconds, then carry on (for example past the claim
             lease, so the original request still runs when the
             reconciler investigates: F06)

Off unless SAFEACT_STACK_FAULT_DIR is set; refused for any schema other
than a disposable test schema, so it can never arm in the demo.
"""

import os
import re
import signal
import tempfile
import time

from pathlib import Path


DISPOSABLE = re.compile(r"safeact_it_[0-9a-f]{32}")
ACTIONS = frozenset({"lost_ack", "crash", "park", "sleep"})


class FailpointTable:

    def __init__(self, directory, schema):
        if not DISPOSABLE.fullmatch(schema):
            raise RuntimeError("Failpoints are for disposable test schemas only")
        self.directory = Path(directory)

    def __call__(self, point, connection):
        armed = self.directory / point
        taken = self.directory / f".{point}.{os.getpid()}.{time.monotonic_ns()}"
        try:
            os.rename(armed, taken)        # atomic: one process wins
        except FileNotFoundError:
            return
        action = taken.read_text().strip()
        with (self.directory / "fired").open("a") as fired:
            fired.write(f"{point} {action} {os.getpid()} {time.time():.3f}\n")
        if action == "lost_ack":
            raise ConnectionError("synthetic lost acknowledgement")
        if action == "crash":
            os.kill(os.getpid(), signal.SIGKILL)
        if action.startswith("sleep:"):
            time.sleep(min(float(action.split(":", 1)[1]), 120.0))
            return
        if action == "park":
            marker = self.directory / f".parked-{os.getpid()}"
            marker.write_text(f"{point} {os.getpid()}\n")
            os.replace(marker, self.directory / "parked")
            while True:
                time.sleep(60)
        raise RuntimeError(f"unknown failpoint action {action!r}")


def from_environment(schema):
    directory = os.environ.get("SAFEACT_STACK_FAULT_DIR")
    if not directory:
        return None
    return FailpointTable(directory, schema)


def arm(directory, point, action):
    """Harness side: arm one failpoint (fires once)."""
    if not re.fullmatch(r"[a-z_]+", point):
        raise ValueError("Invalid failpoint name")
    if action.split(":", 1)[0] not in ACTIONS:
        raise ValueError(f"unknown failpoint action {action!r}")
    fd, temporary = tempfile.mkstemp(prefix=".arming-", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(action)
        os.replace(temporary, Path(directory, point))
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
