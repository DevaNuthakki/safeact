"""Throwaway stack for the console's browser tests (DEMO ONLY).

    .venv/bin/python -m deploy.demo.e2e_server --port 8001 --accounts PATH

Builds a disposable safeact_it_<hex> environment with e2e-scaled
contracts and the synthetic demo accounts, runs the whole stack
(deploy.demo.run_stack) on the given port with Redis database 13, and
writes the demo sign-ins to PATH (mode 600) for the Playwright specs.
On SIGINT or SIGTERM it stops the stack and drops the environment.

Before anything starts it checks that nothing else is using the stack's
resources: it refuses to start if another client is connected to Redis
database 13 or its own port is already listening. Other listening stack
ports and leftover SafeAct processes are reported as a warning. With
--context it writes all of it (and the load average) to a JSON file
kept beside the run's test artifacts, so an intermittent failure can be
diagnosed.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import uuid

from datetime import datetime, timezone
from pathlib import Path

import psycopg
import redis

from deploy.demo.bootstrap import (
    DEMO_ACCOUNTS, Environment, rebuild, teardown_statements,
)
from deploy.demo.profiles import E2E_PROFILES
from deploy.demo.run_stack import Stack
from storage.database import local_admin_settings, local_redis_url


REDIS_DB = 13
# The browser stack's API and console, and the local demo's API and MCP
# ports: none may be taken by a leftover process when a run starts.
WATCHED_PORTS = (8000, 8001, 8002, 5173)
PROCESS_MARKERS = ("unittest", "deploy.demo", "mcp_server", "celery", "uvicorn")


def listening(port):
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def safeact_processes():
    """Other Python processes that look like SafeAct suites or stacks."""

    try:
        output = subprocess.run(
            ["ps", "-axo", "pid=,etime=,command="], capture_output=True,
            text=True, timeout=5, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ["<ps unavailable>"]
    found = []
    for line in output.splitlines():
        pid, _, rest = line.strip().partition(" ")
        _, _, command = rest.strip().partition(" ")
        executable = os.path.basename(command.split(" ", 1)[0]).lower()
        if pid == str(os.getpid()) or not executable.startswith("python"):
            continue
        if any(marker in rest for marker in PROCESS_MARKERS):
            found.append(f"{pid} {rest[:160]}")
    return found


def preflight(broker, port, context_path):
    """Refuse to start on shared resources; record what else is running."""

    others = [
        client for client in broker.client_list()
        if int(client.get("db", -1)) == REDIS_DB
        and client.get("id") != str(broker.client_id())
    ]
    ports = [p for p in WATCHED_PORTS if listening(p)]
    context = {
        "at": datetime.now(timezone.utc).isoformat(),
        "redis_db": REDIS_DB,
        "redis_db_other_clients": [
            {"addr": c.get("addr"), "name": c.get("name"), "age": c.get("age")}
            for c in others
        ],
        "ports_listening": ports,
        "safeact_processes": safeact_processes(),
        "load_average": list(os.getloadavg()),
    }
    if context_path:
        path = Path(context_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(context, indent=2) + "\n", encoding="utf-8")
    problems = []
    if others:
        problems.append(f"{len(others)} other client(s) on Redis database {REDIS_DB}")
    if port in ports:
        problems.append(f"port {port} is already listening")
    if context["safeact_processes"] or set(ports) - {5173}:
        print(
            "e2e preflight warning: other SafeAct processes or stack ports "
            f"are active ({len(context['safeact_processes'])} processes, "
            f"ports {ports}); see the run context file.",
            file=sys.stderr, flush=True,
        )
    if problems:
        raise SystemExit(
            "e2e preflight refused: " + "; ".join(problems)
            + ". A process from an earlier suite or stack is still running."
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--accounts", required=True)
    parser.add_argument(
        "--context", help="where to write the preflight run context (JSON)",
    )
    args = parser.parse_args(argv)

    broker = redis.Redis.from_url(local_redis_url(REDIS_DB))
    preflight(broker, args.port, args.context)
    broker.flushdb()
    admin = psycopg.connect(**local_admin_settings(), autocommit=True)
    env = Environment.disposable(uuid.uuid4().hex)
    stack = None
    try:
        role_passwords, accounts = rebuild(
            admin, env, profiles=E2E_PROFILES, accounts=DEMO_ACCOUNTS
        )
        descriptor = os.open(
            args.accounts, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"schema": env.schema, "accounts": accounts}, handle)

        stack = Stack(
            schema=env.schema,
            roles={"api": env.api, "worker": env.worker,
                   "recoverer": env.recoverer, "advancer": env.advancer},
            passwords=role_passwords, port=args.port, redis_db=REDIS_DB,
            poll_seconds=0.2,
            sse={"sse_poll_seconds": 0.2, "sse_heartbeat_seconds": 5.0,
                 "sse_max_seconds": 30.0},
        ).start()
        print(f"e2e stack ready on {stack.base_url}", flush=True)

        stopping = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stopping.set())
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
        stopping.wait()
    finally:
        if stack is not None:
            stack.stop()
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE usename = ANY(%s)", (list(env.roles),),
        )
        with admin.transaction():
            for statement in teardown_statements(admin, env):
                admin.execute(statement)
        admin.close()
        broker.flushdb()
        try:
            os.unlink(args.accounts)
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
