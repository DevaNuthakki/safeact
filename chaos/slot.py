"""One qualification slot: an isolated place to run scenarios one at a time.

A slot owns:
- the HTTP mock providers, in this (harness) process, so the independent
  effects ledger is read directly and never through any arm;
- one Toxiproxy proxy per provider: every arm reaches the providers only
  through them;
- a Redis database of its own (DBs 2-8; tests use 9-15, the demo 1);
- Redis-kill variants instead own a new authenticated container without persistence;
- a failpoint directory for deterministic commit/ack faults.

Scenarios in a slot run strictly one after another; slots run in
parallel. Each scenario uses fresh resource ids and a fresh provider
account, and the slot resets every mock switch and toxic between
scenarios, so nothing leaks from one scenario into the next.
"""

import fcntl
import os
import shutil
import tempfile

from pathlib import Path

import redis

from chaos import SLOT_REDIS_DBS
from chaos.isolated_redis import IsolatedRedis
from chaos.provider_trace import ObservedProvider
from chaos.quiescence import ProviderBarrier
from chaos.recovery_faults import ObservedOutage
from chaos.workflow_faults import WorkflowPayments
from chaos.toxiproxy import Toxiproxy
from connectors.crm_mock import MockCrmProvider
from connectors.email_mock import MockEmailProvider
from connectors.http_mock.admin_api import FAULTS
from connectors.http_mock.servers import MockProviderServers
from connectors.ledger import EffectsLedger
from storage.database import local_redis_url


PROXY_BASE_PORT = 21000
KINDS = ("payments", "crm", "email")


class Slot:

    def __init__(self, index, *, toxiproxy=None, isolated_broker=False):
        if not 0 <= index < len(SLOT_REDIS_DBS):
            raise ValueError("slot index out of range")
        self.index = index
        self.redis_db = SLOT_REDIS_DBS[index]
        self.toxiproxy = toxiproxy or Toxiproxy()
        self._owns_toxiproxy = toxiproxy is None
        self._lock_fd = None
        self._redis = None
        self.private_broker = IsolatedRedis(index) if isolated_broker else None
        self.ledger = EffectsLedger()
        self.provider_barrier = ProviderBarrier()
        self.mocks = {
            "payments": WorkflowPayments(ledger=self.ledger),
            "crm": MockCrmProvider(ledger=self.ledger),
            "email": MockEmailProvider(ledger=self.ledger),
        }
        self.outages = {kind: ObservedOutage(kind, mock, self.ledger.record_observation)
                        for kind, mock in self.mocks.items()}
        self._defaults = {
            kind: {name: getattr(mock, name) for name in FAULTS[kind]}
            for kind, mock in self.mocks.items()
        }
        self.servers = None
        self.proxies = {}
        self.fault_dir = Path(tempfile.mkdtemp(prefix=f"safeact-slot{index}-"))

    @property
    def proxy_ports(self):
        return {kind: proxy.listen_port for kind, proxy in self.proxies.items()}

    def start(self):
        lock_path = Path(tempfile.gettempdir()) / (
            f"safeact-qualification-{os.getuid()}-slot{self.index}.lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR |
                             getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            raise RuntimeError(f"Qualification slot {self.index} is already in use")
        self._lock_fd = descriptor
        if self.private_broker is not None:
            self.private_broker.start()
        self.servers = MockProviderServers(
            ledger=self.ledger,
            providers=tuple(ObservedProvider(kind, self.mocks[kind],
                            self.ledger.record_observation,
                            barrier=self.provider_barrier) for kind in KINDS),
            stall_seconds=30,
        )
        self.servers.start()
        for offset, kind in enumerate(KINDS):
            self.proxies[kind] = self.toxiproxy.create(
                f"safeact-slot{self.index}-{kind}",
                PROXY_BASE_PORT + 10 * self.index + offset,
                self.servers.ports[kind],
            )
        self.redis().flushdb()
        return self

    @property
    def redis_url(self):
        return self.private_broker.url if self.private_broker is not None else local_redis_url(self.redis_db)

    def redis(self):
        if self._redis is None:
            self._redis = redis.Redis.from_url(self.redis_url,
                socket_connect_timeout=2, socket_timeout=2)
        return self._redis

    def reset(self):
        """Between scenarios: every switch, hold and toxic back to normal."""
        for kind, mock in self.mocks.items():
            for name, value in self._defaults[kind].items():
                setattr(mock, name, value)
            with mock._lock:
                mock._held.clear()
                self.outages[kind].active_id = None
        self.mocks["payments"].reveal_hidden_effects()
        self.mocks["payments"].reset_workflow_fault()
        self.mocks["crm"].reindex()
        for proxy in self.proxies.values():
            self.toxiproxy.clear(proxy.name)
        for armed in self.fault_dir.glob("*"):
            if armed.is_file() and not armed.name.startswith("."):
                if armed.name not in ("fired", "parked"):
                    armed.unlink()

    def set_outage(self, kind, active):
        self.outages[kind].set(active)

    def sample_provider_state(self):
        # Model ordinary provider key-cache expiry, also while lookup is down.
        # Never advance its clock, change retention or remove an unexpired key.
        provider = self.mocks["payments"]
        with provider._lock:
            provider._expire()

    def close_provider_admission(self):
        self.provider_barrier.close()

    def wait_provider_idle(self, timeout):
        self.provider_barrier.wait_idle(timeout)

    def stop(self):
        errors = []
        # Only the lock owner may delete proxies or flush this Redis database.
        if self._lock_fd is not None:
            for kind in KINDS:
                try:
                    self.toxiproxy.delete(f"safeact-slot{self.index}-{kind}")
                except Exception as error:
                    errors.append(repr(error))
            if self.private_broker is None:
                try:
                    self.redis().flushdb()
                except Exception as error:
                    errors.append(repr(error))
        if self.servers is not None:
            try:
                self.servers.stop()
            except Exception as error:
                errors.append(repr(error))
        for client in (self._redis, self.toxiproxy if self._owns_toxiproxy else None):
            if client is not None:
                try:
                    client.close()
                except Exception as error:
                    errors.append(repr(error))
        if self.private_broker is not None:
            try:
                self.private_broker.stop()
            except Exception as error:
                errors.append(type(error).__name__ + ": isolated broker cleanup failed")
        shutil.rmtree(self.fault_dir, ignore_errors=True)
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        if errors:
            raise RuntimeError("Slot cleanup failed: " + "; ".join(errors))
