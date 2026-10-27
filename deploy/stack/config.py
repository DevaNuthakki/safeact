"""Configuration shared by the stack's separate processes (DEMO ONLY).

Secrets come only from a mode-600 credentials file in the demo
bootstrap's format: ``SAFEACT_STACK_CREDENTIALS`` (default
``.demo-credentials``). Database host, port and name come from the local
``.env`` as everywhere else. Non-secret knobs are environment variables:

  SAFEACT_STACK_REDIS_DB            broker database (default 1)
  SAFEACT_STACK_QUEUE               Celery queue (default safeact-stack)
  SAFEACT_STACK_PAYMENTS_URL        mock payments wire API
  SAFEACT_STACK_CRM_URL             mock CRM wire API
  SAFEACT_STACK_EMAIL_URL           mock email wire API
                                    (defaults http://127.0.0.1:8101..8103)
  SAFEACT_STACK_POLL_SECONDS        relay/reconciler/advancer pass (0.5)
  SAFEACT_STACK_LEASE_SECONDS       claim lease (30)
  SAFEACT_STACK_VISIBILITY_TIMEOUT  unacked-message redelivery (120)

Qualification-only Redis credentials may be added to that same private file;
they are refused outside disposable schemas and slot queues.

Provider URLs may name another host (Compose service, Toxiproxy) but
must be plain http with no credentials: these are mocks, never a real
provider.
"""

import os

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import psycopg

from connectors.crm import CrmConnector
from connectors.email import EmailConnector
from connectors.http_providers import (
    HttpCrmProvider,
    HttpEmailProvider,
    HttpPaymentsProvider,
)
from connectors.payments import PaymentsConnector
from storage.database import local_admin_settings, local_redis_url
from storage.connection_pool import ConnectionPool
from deploy.stack.broker_config import qualification_broker_url


COMPONENTS = ("api", "worker", "recoverer", "advancer", "mcp")
DEFAULT_PROVIDER_PORTS = {"payments": 8101, "crm": 8102, "email": 8103,
                          "admin": 8109}


def _provider_url(name):
    value = os.environ.get(
        f"SAFEACT_STACK_{name.upper()}_URL",
        f"http://127.0.0.1:{DEFAULT_PROVIDER_PORTS[name]}",
    )
    parts = urlsplit(value)
    if (parts.scheme != "http" or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment
            or parts.path not in ("", "/")):
        raise RuntimeError(f"Invalid mock {name} provider URL")
    return value.rstrip("/")


def _number(name, default, kind=float, minimum=None):
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = kind(raw)
    if (value <= 0) if minimum is None else (value < minimum):
        raise RuntimeError(f"{name} is out of range")
    return value


@dataclass(frozen=True)
class StackConfig:
    schema: str
    roles: dict
    passwords: dict
    redis_db: int
    queue: str
    provider_urls: dict
    poll_seconds: float
    lease_seconds: int
    visibility_timeout: int
    private_broker_url: str | None = None

    def __repr__(self):  # never print passwords
        return f"StackConfig(schema={self.schema!r}, queue={self.queue!r})"

    @property
    def redis_url(self):
        return self.private_broker_url or local_redis_url(self.redis_db)

    def connect(self, component):
        settings = local_admin_settings()
        role = self.roles[component]
        settings.update(user=role, password=self.passwords[role])
        return psycopg.connect(**settings)

    def connection_pool(self, component):
        """Create in the owning process, after any prefork fork."""
        settings = local_admin_settings()
        role = self.roles[component]
        settings.update(user=role, password=self.passwords[role])
        return ConnectionPool(settings, size=1)

    def connectors(self):
        """Fresh HTTP connectors (create them after any fork)."""
        from deploy.stack.http_trace import from_environment
        observer = from_environment(self.schema)
        return (
            PaymentsConnector(HttpPaymentsProvider(
                self.provider_urls["payments"], observer=observer)),
            CrmConnector(HttpCrmProvider(self.provider_urls["crm"], observer=observer)),
            EmailConnector(HttpEmailProvider(self.provider_urls["email"], observer=observer)),
        )


def credentials_path():
    from deploy.demo.bootstrap import CREDENTIALS

    return Path(os.environ.get("SAFEACT_STACK_CREDENTIALS") or CREDENTIALS)


def load(credentials=None):
    from deploy.demo.bootstrap import read_credentials

    values = read_credentials(credentials or credentials_path())
    roles = {
        component: values[f"SAFEACT_DEMO_{name}_ROLE"]
        for component, name in (
            ("api", "API"), ("worker", "WORKER"), ("recoverer", "RECOVERER"),
            ("advancer", "ADVANCER"), ("mcp", "MCP"),
        )
    }
    passwords = {
        roles[component]: values[f"SAFEACT_DEMO_{name}_PASSWORD"]
        for component, name in (
            ("api", "API"), ("worker", "WORKER"), ("recoverer", "RECOVERER"),
            ("advancer", "ADVANCER"), ("mcp", "MCP"),
        )
    }
    queue = os.environ.get("SAFEACT_STACK_QUEUE", "safeact-stack")
    if not queue.replace("-", "").replace("_", "").isalnum():
        raise RuntimeError("Invalid queue name")
    redis_db = _number("SAFEACT_STACK_REDIS_DB", 1, int, minimum=0)
    return StackConfig(
        schema=values["SAFEACT_DEMO_SCHEMA"],
        roles=roles,
        passwords=passwords,
        redis_db=redis_db,
        private_broker_url=qualification_broker_url(values, redis_db, queue),
        queue=queue,
        provider_urls={k: _provider_url(k) for k in ("payments", "crm",
                                                     "email")},
        poll_seconds=_number("SAFEACT_STACK_POLL_SECONDS", 0.5),
        lease_seconds=int(_number("SAFEACT_STACK_LEASE_SECONDS", 30, int)),
        visibility_timeout=int(_number(
            "SAFEACT_STACK_VISIBILITY_TIMEOUT", 120, int)),
    )
