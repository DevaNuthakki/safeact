"""Run the three mock provider wire APIs plus the admin API on loopback.

Used in-process by tests and the threaded stack, and as its own process
by ``python -m deploy.stack.providers``. Ports default to 0 (any free
port); the chosen ports are available once ``start()`` returns.
"""

import socket
import threading
import time

import uvicorn

from connectors.crm_mock import MockCrmProvider
from connectors.email_mock import MockEmailProvider
from connectors.http_mock.admin_api import admin_app
from connectors.http_mock.provider_api import (
    Stall,
    crm_app,
    email_app,
    payments_app,
)
from connectors.ledger import EffectsLedger
from connectors.payments_mock import MockPaymentsProvider


LOOPBACK = "127.0.0.1"


def _bound_socket(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    return sock


class MockProviderServers:

    def __init__(self, *, ledger=None, providers=None, host=LOOPBACK,
                 ports=None, stall_seconds=120.0):
        if host != LOOPBACK:
            raise ValueError("Mock providers bind to loopback only")
        self.ledger = ledger or EffectsLedger()
        if providers is None:
            providers = (
                MockPaymentsProvider(ledger=self.ledger),
                MockCrmProvider(ledger=self.ledger),
                MockEmailProvider(ledger=self.ledger),
            )
        self.payments, self.crm, self.email = providers
        self.host = host
        self.stall = Stall(stall_seconds)
        self._ports = dict(ports or {})
        self.ports = {}
        self._servers = []
        self._threads = []

    def url(self, kind):
        return f"http://{self.host}:{self.ports[kind]}"

    @property
    def urls(self):
        return {kind: self.url(kind) for kind in ("payments", "crm", "email")}

    def start(self):
        apps = {
            "payments": payments_app(self.payments, self.stall),
            "crm": crm_app(self.crm, self.stall),
            "email": email_app(self.email, self.stall),
            "admin": admin_app(self.ledger, {
                "payments": self.payments, "crm": self.crm,
                "email": self.email,
            }),
        }
        for kind, app in apps.items():
            sock = _bound_socket(self.host, self._ports.get(kind, 0))
            self.ports[kind] = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(
                app, log_level="warning", access_log=False, lifespan="off",
                timeout_graceful_shutdown=2, log_config=None,
            ))
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [sock]},
                name=f"mock-{kind}", daemon=True,
            )
            self._servers.append(server)
            self._threads.append(thread)
            thread.start()
        deadline = time.monotonic() + 15
        while not all(server.started for server in self._servers):
            if time.monotonic() > deadline:
                raise RuntimeError("mock provider servers did not start")
            time.sleep(0.02)
        return self

    def stop(self):
        self.stall.release_all()
        for server in self._servers:
            server.should_exit = True
        for thread in self._threads:
            thread.join(timeout=10)
