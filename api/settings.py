"""API process settings.

The API connects to PostgreSQL as one restricted login role for one
lifecycle schema. For the local demo both come from the mode-600
``.demo-credentials`` file written by ``deploy.demo.bootstrap``.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from storage import durable_lifecycle_sql as lsql


@dataclass(frozen=True)
class Settings:
    schema: str
    role: str
    password: str = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 55432
    dbname: str = "safeact_dev"
    # Origins allowed to send state-changing requests (the console's
    # dev server). Same-origin and non-browser requests are allowed;
    # the CSRF token is still checked in PostgreSQL either way.
    allowed_origins: tuple = (
        "http://localhost:5173", "http://127.0.0.1:5173",
    )
    # Live updates (GET /events): how often the stream asks PostgreSQL
    # for changes, how often it sends a keep-alive, how long one stream
    # lives before the client reconnects with Last-Event-ID, and how
    # many streams one session may hold open.
    sse_poll_seconds: float = 1.0
    sse_heartbeat_seconds: float = 15.0
    sse_max_seconds: float = 300.0
    sse_max_streams: int = 3

    def __post_init__(self):
        if self.schema == "public":
            raise ValueError("The API never serves the public schema")
        lsql._schema(self.schema)
        lsql._role(self.role)
        if not self.password:
            raise ValueError("Missing API database password")

    def connect_kwargs(self):
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.role,
            "password": self.password,
            "connect_timeout": 5,
            "application_name": "safeact-api",
            "options": "-c statement_timeout=5000",
        }


def demo_settings(credentials=None):
    """Settings for the local ``safeact_demo`` environment."""

    from deploy.demo.bootstrap import CREDENTIALS, read_credentials
    from storage.database import local_admin_settings

    # The separate-process stack points every component at one file.
    credentials = credentials or os.environ.get("SAFEACT_STACK_CREDENTIALS")
    values = read_credentials(Path(credentials) if credentials else CREDENTIALS)
    admin = local_admin_settings()
    return Settings(
        schema=values["SAFEACT_DEMO_SCHEMA"],
        role=values["SAFEACT_DEMO_API_ROLE"],
        password=values["SAFEACT_DEMO_API_PASSWORD"],
        host=admin["host"],
        port=admin["port"],
        dbname=admin["dbname"],
    )
