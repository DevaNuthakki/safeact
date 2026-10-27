"""MCP server process settings.

The server connects to PostgreSQL as the restricted MCP login role for
one lifecycle schema and listens on 127.0.0.1 only. For the local demo
both come from the mode-600 ``.demo-credentials`` file written by
``deploy.demo.bootstrap``.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from storage import durable_lifecycle_sql as lsql


BIND_HOST = "127.0.0.1"
MAX_WAIT_SECONDS = 10


@dataclass(frozen=True)
class McpSettings:
    schema: str
    role: str
    password: str = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 55432
    dbname: str = "safeact_dev"
    # get_action_status may wait for a change, at most this long, asking
    # PostgreSQL this often.
    max_wait_seconds: float = MAX_WAIT_SECONDS
    wait_poll_seconds: float = 0.5

    def __post_init__(self):
        if self.schema == "public":
            raise ValueError("The MCP server never serves the public schema")
        lsql._schema(self.schema)
        lsql._role(self.role)
        if not self.password:
            raise ValueError("Missing MCP database password")
        if not 0 < self.max_wait_seconds <= MAX_WAIT_SECONDS:
            raise ValueError("max_wait_seconds must be within 0..10")

    def connect_kwargs(self):
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.role,
            "password": self.password,
            "connect_timeout": 5,
            "application_name": "safeact-mcp",
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
    return McpSettings(
        schema=values["SAFEACT_DEMO_SCHEMA"],
        role=values["SAFEACT_DEMO_MCP_ROLE"],
        password=values["SAFEACT_DEMO_MCP_PASSWORD"],
        host=admin["host"],
        port=admin["port"],
        dbname=admin["dbname"],
    )
