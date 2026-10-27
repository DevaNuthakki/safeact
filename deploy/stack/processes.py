"""Run the stack as separate OS processes (DEMO ONLY).

    .venv/bin/python -m deploy.demo.run_stack --processes

One process per component, each with its own database role:
- mock providers (HTTP wire APIs + harness-only admin API),
- a real Celery worker (``celery -A deploy.stack.worker worker``,
  prefork pool),
- relay, reconciler and workflow advancer loops,
- the API (uvicorn) and the guarded MCP server.

They share only PostgreSQL, Redis and HTTP, as they would in
deployment, so a process can be killed on its own (M5.5) and the
network between a worker and a provider can be cut (Toxiproxy, M5.9).
Children receive their configuration through the SAFEACT_STACK_*
variables (deploy/stack/config.py); secrets stay in the credentials file.
"""

import os
import signal
import socket
import subprocess
import sys
import time

from pathlib import Path

import httpx2 as httpx

from deploy.stack.config import DEFAULT_PROVIDER_PORTS


ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable
CELERY = str(Path(sys.executable).with_name("celery"))
COMPONENTS = ("providers", "worker", "relay", "reconciler", "advancer",
              "api", "mcp")


class ProcessStack:

    def __init__(self, *, credentials=None, redis_db=1, queue="safeact-stack",
                 api_port=8000, mcp_port=8002, provider_ports=None,
                 poll_seconds=0.5, concurrency=2, components=COMPONENTS,
                 plain_providers=False, extra_env=None, log_dir=None,
                 metrics_ports=None):
        unknown = set(components) - set(COMPONENTS)
        if unknown:
            raise ValueError(f"unknown components {sorted(unknown)}")
        self.components = tuple(components)
        self.api_port = api_port
        self.mcp_port = mcp_port
        self.ports = dict(DEFAULT_PROVIDER_PORTS, **(provider_ports or {}))
        self.concurrency = concurrency
        self.plain_providers = plain_providers
        self.log_dir = Path(log_dir) if log_dir else None
        self.env = dict(os.environ)
        self.env.update({
            "SAFEACT_STACK_REDIS_DB": str(redis_db),
            "SAFEACT_STACK_QUEUE": queue,
            "SAFEACT_STACK_POLL_SECONDS": str(poll_seconds),
            "SAFEACT_STACK_PAYMENTS_URL":
                f"http://127.0.0.1:{self.ports['payments']}",
            "SAFEACT_STACK_CRM_URL": f"http://127.0.0.1:{self.ports['crm']}",
            "SAFEACT_STACK_EMAIL_URL":
                f"http://127.0.0.1:{self.ports['email']}",
            "PYTHONUNBUFFERED": "1",
        })
        if credentials is not None:
            self.env["SAFEACT_STACK_CREDENTIALS"] = str(credentials)
        self.env.update(extra_env or {})
        # Per-component metrics exporters (127.0.0.1); the prefork worker
        # uses prometheus_client's multiprocess directory.
        self.metrics_ports = dict(metrics_ports or {})
        self.component_env = {
            component: {"SAFEACT_METRICS_PORT": str(port)}
            for component, port in self.metrics_ports.items()
        }
        if "worker" in self.metrics_ports:
            import tempfile
            self._multiproc = tempfile.TemporaryDirectory(
                prefix="safeact-metrics-")
            self.component_env["worker"]["PROMETHEUS_MULTIPROC_DIR"] = (
                self._multiproc.name)
        self.processes = {}
        self._logs = []

    @property
    def admin_url(self):
        return f"http://127.0.0.1:{self.ports['admin']}"

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.api_port}"

    def command(self, component):
        if component == "providers":
            command = [PYTHON, "-m", "deploy.stack.providers"] + [
                arg for kind, port in self.ports.items()
                for arg in (f"--{kind}-port", str(port))
            ]
            return command + (["--plain"] if self.plain_providers else [])
        if component == "worker":
            return [CELERY, "-A", "deploy.stack.worker", "worker",
                    "--pool=prefork", f"--concurrency={self.concurrency}",
                    "--loglevel=WARNING", "--without-heartbeat",
                    "--without-mingle", "--without-gossip"]
        if component in ("relay", "reconciler", "advancer"):
            return [PYTHON, "-m", "deploy.stack.loops", component]
        if component == "api":
            return [PYTHON, "-m", "uvicorn", "--factory", "api.app:demo_app",
                    "--host", "127.0.0.1", "--port", str(self.api_port),
                    "--log-level", "warning", "--no-access-log"]
        if component == "mcp":
            return [PYTHON, "-m", "mcp_server.server",
                    "--port", str(self.mcp_port)]
        raise ValueError(component)

    def spawn(self, component):
        if self.log_dir is not None:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            out = open(self.log_dir / f"{component}.log", "ab")
            self._logs.append(out)
        else:
            out = subprocess.DEVNULL
        process = subprocess.Popen(
            self.command(component), cwd=ROOT,
            env={**self.env, **self.component_env.get(component, {})},
            stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
        )
        self.processes[component] = process
        return process

    def start(self, timeout=30):
        for component in self.components:
            self.spawn(component)
            if component == "providers":
                self._wait_http(self.admin_url + "/healthz", timeout)
        if "api" in self.components:
            self._wait_http(self.base_url + "/healthz", timeout)
        if "mcp" in self.components:
            self._wait_port(self.mcp_port, timeout)
        return self

    def _wait_port(self, port, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_alive()
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    return
            time.sleep(0.1)
        raise RuntimeError(f"port {port} did not open")

    def _wait_http(self, url, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_alive()
            try:
                if httpx.get(url, timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise RuntimeError(f"{url} did not become ready")

    def _check_alive(self):
        for component, process in self.processes.items():
            if process.poll() is not None:
                raise RuntimeError(
                    f"{component} exited with {process.returncode}"
                )

    def kill(self, component, sig=signal.SIGKILL):
        """Kill one component's whole process group (worker children too)."""
        process = self.processes[component]
        os.killpg(process.pid, sig)
        process.wait(timeout=15)

    def stop(self):
        for component in reversed(self.components):
            process = self.processes.get(component)
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for component, process in self.processes.items():
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        for log in self._logs:
            log.close()
        if getattr(self, "_multiproc", None) is not None:
            self._multiproc.cleanup()
