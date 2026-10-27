"""Disposable API environment for integration tests.

Builds a ``safeact_it_<hex32>`` environment with the same rebuild the
demo bootstrap uses (head schema, mock contracts, component roles, the
synthetic demo accounts) and serves it through the real FastAPI app as
the restricted API role. Not a test module (no ``test_`` prefix).

Clients use an https base URL: the session cookies are Secure, and a
standards-following cookie jar never sends them over plain http.
"""

import uuid

import psycopg
from fastapi.testclient import TestClient
from psycopg import sql

from api.app import create_app
from api.settings import Settings
from deploy.demo.bootstrap import (
    DEMO_ACCOUNTS, Environment, issue_agent_keys, rebuild,
    teardown_statements,
)
from storage.database import local_admin_settings
from storage.state_projection import projection_drift


class ApiEnvironment:

    def __init__(self, accounts=DEMO_ACCOUNTS, **settings):
        self.env = Environment.disposable(uuid.uuid4().hex)
        self.accounts = accounts
        self.overrides = settings
        self.admin = None

    @property
    def schema(self):
        return self.env.schema

    def start(self):
        self.admin = psycopg.connect(**local_admin_settings(), autocommit=True)
        self.role_passwords, self.passwords = rebuild(
            self.admin, self.env, accounts=self.accounts
        )
        admin = local_admin_settings()
        self.settings = Settings(
            schema=self.env.schema, role=self.env.api,
            password=self.role_passwords[self.env.api],
            host=admin["host"], port=admin["port"], dbname=admin["dbname"],
            **self.overrides,
        )
        self.app = create_app(self.settings)
        # One agent key per service account (plaintext only in memory).
        self.agent_keys = issue_agent_keys(self.admin, self.env, self.accounts)
        return self

    def cleanup(self):
        if self.admin is None:
            return
        if hasattr(self, "app") and self.app.state.connection_pool is not None:
            self.app.state.connection_pool.close()
        # Every test class leaves the projection equal to the log.
        try:
            drift = projection_drift(self.admin, self.env.schema)
        except psycopg.Error as error:
            drift = [f"drift check failed: {type(error).__name__}"]
        try:
            self.admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE usename = ANY(%s)", (list(self.env.roles),),
            )
            with self.admin.transaction():
                for statement in teardown_statements(self.admin, self.env):
                    self.admin.execute(statement)
        finally:
            self.admin.close()
        if drift:
            raise AssertionError(
                f"Current-state projection drift: {drift[:3]}"
            )

    # -- helpers -----------------------------------------------------

    def client(self, **kwargs):
        kwargs.setdefault("base_url", "https://testserver")
        kwargs.setdefault("raise_server_exceptions", False)
        return TestClient(self.app, **kwargs)

    def signed_in(self, username):
        """A client signed in as ``username``; returns (client, csrf)."""

        client = self.client()
        response = client.post("/auth/login", json={
            "username": username, "password": self.passwords[username],
        })
        if response.status_code != 200:
            # Status and error code only: never the password or cookies.
            try:
                code = response.json()["error"]["code"]
            except (ValueError, KeyError, TypeError):
                code = "<no error code>"
            raise RuntimeError(
                f"sign-in failed for {username}: "
                f"HTTP {response.status_code} {code}"
            )
        return client, response.json()["csrf_token"]

    def connect_api(self):
        """A raw connection as the API login role (direct-SQL tests)."""

        settings = self.settings.connect_kwargs()
        return psycopg.connect(**settings, autocommit=True)

    def connect_mcp(self, **kwargs):
        """A connection as the MCP login role (agent SQL tests)."""

        kwargs.setdefault("autocommit", True)
        return self.connect_role(self.env.mcp, **kwargs)

    def connect_role(self, role, **kwargs):
        """A connection as one of the environment's component roles."""

        settings = local_admin_settings()
        settings.update(user=role, password=self.role_passwords[role])
        settings.update(kwargs)
        return psycopg.connect(**settings)

    def table(self, name):
        return sql.SQL("{}.{}").format(
            sql.Identifier(self.env.schema), sql.Identifier(name)
        )

    def rows(self, query, params=()):
        return self.admin.execute(query, params).fetchall()

    def count(self, table, where=sql.SQL("true"), params=()):
        return self.admin.execute(
            sql.SQL("SELECT count(*) FROM {} WHERE {}").format(
                self.table(table), where
            ), params,
        ).fetchone()[0]

    def principal_id(self, username):
        return self.admin.execute(
            sql.SQL("SELECT principal_id FROM {} WHERE username = %s")
            .format(self.table("safeact_principals")), (username,),
        ).fetchone()[0]
