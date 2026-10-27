"""The Celery worker as its own process (DEMO ONLY).

    celery -A deploy.stack.worker worker --pool=prefork --concurrency=2

Same task, broker settings and ExecutionWorker as the threaded stack,
over the HTTP mock providers. The execution worker and its HTTP clients
are built lazily in each process, after Celery's prefork fork, so no
connection pool is shared between processes. Configuration:
deploy/stack/config.py.
"""

import os
import re
import time

from pathlib import Path

import billiard

from api import logging as api_logging
from deploy.stack import broker_trace, execution_trace, failpoints
from deploy.stack.config import load
from observability import metrics
from storage.experimental_celery import make_app
from storage.experimental_durable_execution import ExecutionWorker


# Prefork children are forked, as on Linux. billiard defaults to spawn
# on macOS, where a fresh child rebuilds the app without this module's
# task or the worker's tracer state, and every task fails.
billiard.set_start_method("fork", force=True)

api_logging.configure()
config = load()
# The pool parent exports every child's counts (multiprocess mode, set up
# by the environment before this module is imported). Forked children
# never import this module again, so only the parent serves.
metrics.serve()

app = make_app(
    config.redis_url, queue=config.queue,
    visibility_timeout=config.visibility_timeout,
)
broker_trace.install(app, config.schema, config.queue, role="worker")

_workers = {}

DISPOSABLE = re.compile(r"safeact_it_[0-9a-f]{32}")


def _failpoint():
    """Harness-only: park this process at one named point (M5.5).

    SAFEACT_STACK_FAILPOINT names an ExecutionWorker fault point (for
    example after_claim_commit or after_provider_call). On reaching it the
    process writes "<point> <pid>" to SAFEACT_STACK_FAILPOINT_MARKER and
    waits there until it is killed. Refused for any schema other than a
    disposable test schema, so it can never arm in the demo.
    """

    point = os.environ.get("SAFEACT_STACK_FAILPOINT")
    if not point:
        return None
    marker = os.environ.get("SAFEACT_STACK_FAILPOINT_MARKER")
    if not marker or not DISPOSABLE.fullmatch(config.schema):
        raise RuntimeError("Failpoints are for disposable test schemas only")

    def hook(name, connection):
        if name == point:
            Path(marker).write_text(f"{name} {os.getpid()}\n")
            while True:
                time.sleep(60)
    return hook


def _worker():
    pid = os.getpid()
    worker = _workers.get(pid)
    if worker is None:
        pool = config.connection_pool("worker")
        worker = ExecutionWorker(
            connect=lambda: config.connect("worker"), schema=config.schema,
            connection_scope=pool.connection,
            connectors=config.connectors(),
            lease_seconds=config.lease_seconds,
            fault_hook=execution_trace.from_environment(config.schema,
                _failpoint() or failpoints.from_environment(config.schema)),
        )
        _workers.clear()
        _workers[pid] = worker
    return worker


app.safeact_execute = lambda task: _worker().execute(task)
