"""The relay, reconciler or workflow advancer as its own process (DEMO ONLY).

    python -m deploy.stack.loops relay
    python -m deploy.stack.loops reconciler
    python -m deploy.stack.loops advancer

Each runs one pass every SAFEACT_STACK_POLL_SECONDS as its own database
role until SIGTERM or SIGINT, exactly as the threaded stack does.
Configuration: deploy/stack/config.py.
"""

import argparse
import signal
import sys
import threading

from api import logging as api_logging
from deploy.stack import broker_trace, execution_trace, failpoints
from deploy.stack.config import load
from observability import metrics
from storage.experimental_celery import CeleryPublisher, make_app
from storage.experimental_durable_execution import relay_once
from storage.experimental_recovery_worker import Reconciler
from storage.experimental_workflow import WorkflowAdvancer


def step(name, config):
    if name == "relay":
        pool = config.connection_pool("worker")
        app = make_app(config.redis_url, queue=config.queue,
                       visibility_timeout=config.visibility_timeout)
        broker_trace.install(app, config.schema, config.queue, role="publisher")
        publisher = CeleryPublisher(app, queue=config.queue)

        def relay():
            with pool.connection() as connection:
                relay_once(connection, config.schema, publisher)
        return relay

    if name == "reconciler":
        pool = config.connection_pool("recoverer")
        return Reconciler(
            connect=lambda: config.connect("recoverer"),
            schema=config.schema, connectors=config.connectors(),
            connection_scope=pool.connection,
            retry_authorized=True,
            fault_hook=execution_trace.from_environment(config.schema,
                failpoints.from_environment(config.schema)),
        ).reconcile_once

    if name == "advancer":
        pool = config.connection_pool("advancer")
        return WorkflowAdvancer(
            connect=lambda: config.connect("advancer"), schema=config.schema,
            connection_scope=pool.connection,
        ).advance_once

    raise ValueError(f"unknown loop {name!r}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("loop", choices=("relay", "reconciler", "advancer"))
    args = parser.parse_args(argv)

    from deploy.demo.run_stack import Loop

    api_logging.configure()
    config = load()
    # The reconciler (recovery role) also exports the durable gauges.
    metrics.serve(metrics.DatabaseCollector(
        lambda: config.connect("recoverer"), config.schema,
    ) if args.loop == "reconciler" else None)
    loop = Loop(args.loop, step(args.loop, config), config.poll_seconds)
    stopping = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    loop.start()
    print(f"{args.loop} running", flush=True)
    stopping.wait()
    loop.stop()
    loop.join(timeout=15)
    return 0


if __name__ == "__main__":
    sys.exit(main())
