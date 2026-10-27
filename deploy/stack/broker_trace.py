"""Disposable-only Celery signals; ids and delivery metadata, never task payloads."""

import base64
import fcntl
import json
import math
import os
import stat
import time

from pathlib import Path
from uuid import UUID

from deploy.stack.http_trace import HttpTrace


ERROR = b"SAFEACT_BROKER_TRACE_ERROR\n"
TASK = "safeact.execute_operation"
IDS = ("operation_id", "outbox_id", "delivery_id", "task_id")
TASK_STATUSES = frozenset({"succeeded", "failed_confirmed", "unknown", "not_claimable", "invalid_intent", "uncertain", "late_evidence"})
EVENTS = frozenset({"publish_started", "publish_returned", "task_received", "task_started", "task_returned"})


def identity(args, task_id):
    if not isinstance(args, (tuple, list)) or len(args) != 3:
        raise ValueError("Expected three durable task ids")
    values = [*args, task_id]
    if any(not isinstance(value, str) or str(UUID(value)) != value for value in values):
        raise ValueError("Invalid task id")
    return dict(zip(IDS, values))


def queued_message(envelope, queue):
    """Decode the pinned Kombu JSON envelope and keep only its four ids."""
    if not isinstance(envelope, bytes) or len(envelope) > 65536:
        raise ValueError("Invalid broker envelope")
    message = json.loads(envelope)
    headers, properties = message["headers"], message["properties"]
    if (headers["task"] != TASK or message["content-type"] != "application/json"
            or message["content-encoding"] != "utf-8" or properties["body_encoding"] != "base64"
            or properties["correlation_id"] != headers["id"]
            or properties["delivery_info"]["routing_key"] != queue):
        raise ValueError("Unexpected broker envelope")
    body = json.loads(base64.b64decode(message["body"], validate=True))
    if not isinstance(body, list) or len(body) != 3 or body[1] != {}:
        raise ValueError("Unexpected broker task body")
    return identity(body[0], headers["id"])


def _validate(row):
    allowed = {*IDS, "event", "source", "pid", "at", "monotonic_ns", "queue", "redelivered"}
    if "task_status" in row:
        allowed.add("task_status")
        if row.get("event") != "task_returned" or row["task_status"] not in TASK_STATUSES:
            raise ValueError("Invalid task status observation")
    if (set(row) != allowed or row["event"] not in EVENTS or row["source"] != "broker"
            or type(row["pid"]) is not int or row["pid"] <= 0
            or type(row["monotonic_ns"]) is not int or row["monotonic_ns"] <= 0
            or type(row["at"]) not in (int, float) or not math.isfinite(row["at"])
            or type(row["redelivered"]) is not bool
            or not isinstance(row["queue"], str) or not row["queue"] or len(row["queue"]) > 128):
        raise ValueError("Invalid broker observation")
    identity([row[key] for key in IDS[:3]], row["task_id"])


class BrokerTrace(HttpTrace):
    def __call__(self, event, **details):
        row = dict(details, event=event, source="broker", pid=os.getpid(),
                   at=time.time(), monotonic_ns=time.monotonic_ns())
        _validate(row)
        data = (json.dumps(row, sort_keys=True) + "\n").encode()
        if len(data) > 4096:
            raise ValueError("Broker observation exceeds its size limit")
        fd = os.open(self.directory / f"broker-{os.getpid()}.jsonl",
            os.O_APPEND | os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise RuntimeError("Broker trace must be a private regular file")
            fcntl.flock(fd, fcntl.LOCK_EX)
            if os.write(fd, data) != len(data):
                raise OSError("Incomplete broker trace write")
        finally:
            os.close(fd)


def _failed():
    try:
        os.write(2, ERROR)
    except OSError:
        pass


def install(app, schema, queue, *, role, signals=None):
    directory = os.environ.get("SAFEACT_STACK_BROKER_TRACE_DIR")
    if not directory:
        return None
    writer = BrokerTrace(directory, schema)
    if signals is None:
        from celery import signals
    connections = []

    def connect(name, callback):
        signal = getattr(signals, name)
        signal.connect(callback, weak=False)
        connections.append((signal, callback))

    def publish(event):
        def observe(sender=None, headers=None, body=None, routing_key=None, **kwargs):
            if sender != TASK or routing_key != queue:
                return
            try:
                writer(event, **identity(body[0], headers["id"]), queue=queue, redelivered=False)
            except Exception:
                _failed()
        return observe

    def execute(event):
        def observe(sender=None, task=None, task_id=None, args=None, **kwargs):
            if task is None or task.app is not app or task.name != TASK:
                return
            try:
                info = task.request.delivery_info
                status = {}
                if (event == "task_returned" and kwargs.get("state") == "SUCCESS"
                        and type(kwargs.get("retval")) is str and kwargs["retval"] in TASK_STATUSES):
                    status["task_status"] = kwargs["retval"]
                writer(event, **identity(args, task_id), queue=queue, **status,
                       redelivered=info.get("redelivered", False))
            except Exception:
                _failed()
        return observe

    def received(sender=None, request=None, **kwargs):
        try:
            if request is None or request.name != TASK or request.task.app is not app:
                return
            writer("task_received", **identity(request.args, request.id), queue=queue,
                   redelivered=request.delivery_info.get("redelivered", False))
        except Exception:
            _failed()

    if role == "publisher":
        connect("before_task_publish", publish("publish_started"))
        connect("after_task_publish", publish("publish_returned"))
    elif role == "worker":
        connect("task_received", received)
        connect("task_prerun", execute("task_started"))
        connect("task_postrun", execute("task_returned"))
    else:
        raise ValueError("Invalid broker observer role")
    return connections


def read_trace(directory, since_ns):
    rows, seen = [], set()
    for path in sorted(Path(directory).glob("broker-*.jsonl")):
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            for number, line in enumerate(stream, 1):
                try:
                    row = json.loads(line)
                    _validate(row)
                    key = (row["pid"], row["monotonic_ns"], row["event"], row["task_id"])
                    if key in seen:
                        raise ValueError("Duplicate broker observation")
                    seen.add(key)
                except (KeyError, TypeError, ValueError) as error:
                    raise RuntimeError(f"Invalid broker trace at {path.name}:{number}") from error
                if row["monotonic_ns"] >= since_ns:
                    rows.append(row)
    return sorted(rows, key=lambda row: row["monotonic_ns"])
