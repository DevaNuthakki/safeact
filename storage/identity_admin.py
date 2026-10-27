"""Administrative identity seeding (local demo and tests only).

Principals, roles, credentials and agent keys are created by the
database administrator, never by the API or the MCP server: the API
role can only log in, log out and ask who it is. Passwords are hashed inside PostgreSQL with
pgcrypto bcrypt (cost 12) from a bound parameter, inside a transaction
whose statement and parameter logging is switched off, so a plaintext
password never reaches a server log.
"""

import hashlib
import secrets

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from psycopg import sql

from storage import durable_lifecycle_sql as lsql
from storage.lifecycle_sql.v0011 import BCRYPT_COST, ROLES
from storage.lifecycle_sql.v0016 import REVOCATION_REASONS, SCOPES

AGENT_KEY_PREFIX = "sak_"
AGENT_KEY_MAX_LIFETIME = timedelta(days=30)


# Applied to the API login role (ALTER ROLE ... SET) and, as SET LOCAL,
# to any administrator transaction that handles a plaintext password.
# Together they keep statements, their parameters and slow-query or
# sampled-transaction logging from ever recording a secret.
NO_SECRET_LOGGING = {
    "log_statement": "none",
    "log_duration": "off",
    "log_min_duration_statement": "-1",
    "log_min_duration_sample": "-1",
    "log_transaction_sample_rate": "0",
    "log_parameter_max_length": "0",
    "log_parameter_max_length_on_error": "0",
    "log_min_error_statement": "panic",
}


# An abandoned application transaction is ended after this long, so it
# cannot hold the change feed's horizon (and so live updates) back for
# longer. Every application transaction is short: none waits on a
# provider while open.
APPLICATION_IDLE_TIMEOUT = "60s"


def application_role_statements(role):
    return [sql.SQL(
        "ALTER ROLE {} SET idle_in_transaction_session_timeout = {}"
    ).format(lsql._role(role), sql.Literal(APPLICATION_IDLE_TIMEOUT))]


def role_logging_statements(role):
    return [
        sql.SQL("ALTER ROLE {} SET {} = {}").format(
            lsql._role(role), sql.Identifier(name), sql.Literal(value)
        )
        for name, value in NO_SECRET_LOGGING.items()
    ]


def suppress_logging(connection):
    """SET LOCAL every logging switch off; needs an open transaction."""

    for name, value in NO_SECRET_LOGGING.items():
        connection.execute(sql.SQL("SET LOCAL {} = {}").format(
            sql.Identifier(name), sql.Literal(value)
        ))


def create_principal(
    connection, schema, *, tenant_id, username, display_name,
    kind="human", roles=(), password=None, principal_id=None, grants=(),
):
    """Insert one principal with its roles, grants and optional password.

    ``grants`` are exact (connector, provider account, action, resource,
    contract version) tuples.

    Runs in the caller's open transaction (an administrator). Returns
    the principal id.
    """

    if any(role not in ROLES for role in roles):
        raise ValueError("Unknown role")
    if password is not None and (
        type(password) is not str or not 1 <= len(password.encode()) <= 72
    ):
        raise ValueError("Passwords must be 1 to 72 bytes")

    s = lsql._schema(schema)
    principal_id = UUID(str(principal_id)) if principal_id else uuid4()

    connection.execute(sql.SQL(
        "INSERT INTO {}.safeact_principals "
        "(principal_id, tenant_id, username, kind, display_name, demo) "
        "VALUES (%s, %s, %s, %s, %s, %s)"
    ).format(s), (
        principal_id, tenant_id, username, kind, display_name,
        username.startswith("demo-"),
    ))
    for role in roles:
        connection.execute(sql.SQL(
            "INSERT INTO {}.safeact_role_grants (principal_id, role) "
            "VALUES (%s, %s)"
        ).format(s), (principal_id, role))

    for connector, account, action, resource, version in grants:
        connection.execute(sql.SQL(
            "INSERT INTO {}.safeact_action_grants (principal_id, "
            "connector_id, provider_account_id, action_type, resource_id, "
            "contract_version) VALUES (%s, %s, %s, %s, %s, %s)"
        ).format(s), (principal_id, connector, account, action, resource,
                      version))

    if password is not None:
        suppress_logging(connection)
        connection.execute(sql.SQL(
            "INSERT INTO {}.safeact_credentials (principal_id, password_hash) "
            "VALUES (%s, public.crypt(%s, public.gen_salt('bf', {})))"
        ).format(s, sql.Literal(BCRYPT_COST)), (principal_id, password))

    return principal_id


@dataclass(frozen=True)
class AgentKey:
    """A newly issued agent key. The plaintext exists only here, once."""

    key_id: UUID
    expires_at: datetime
    key: str = field(repr=False)

    def __repr__(self):
        return f"AgentKey(key_id={self.key_id}, expires_at={self.expires_at})"


def issue_agent_key(
    connection, schema, *, principal_id, scopes, label, created_by,
    lifetime=AGENT_KEY_MAX_LIFETIME,
):
    """Issue a key for a service principal; return it once.

    The key is 32 random bytes generated here; only its SHA-256 is sent
    to PostgreSQL, so the plaintext never reaches the server or its
    logs. The table's CHECKs pin the scopes (never reviewer) and the
    30-day limit; a trigger refuses a non-service principal. Runs in the
    caller's open transaction (an administrator).
    """

    scopes = tuple(sorted(set(scopes)))
    if not scopes or any(scope not in SCOPES for scope in scopes):
        raise ValueError("Agent key scopes must come from " + ", ".join(SCOPES))
    if not timedelta(0) < lifetime <= AGENT_KEY_MAX_LIFETIME:
        raise ValueError("Agent keys live for at most 30 days")

    key = AGENT_KEY_PREFIX + secrets.token_hex(32)
    key_id = uuid4()
    expires_at = connection.execute(sql.SQL(
        "INSERT INTO {}.safeact_agent_keys (key_id, principal_id, "
        "key_sha256, scopes, label, created_by, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, now() + %s) RETURNING expires_at"
    ).format(lsql._schema(schema)), (
        key_id, UUID(str(principal_id)),
        hashlib.sha256(key.encode()).digest(), list(scopes), label,
        created_by, lifetime,
    )).fetchone()[0]
    return AgentKey(key_id=key_id, expires_at=expires_at, key=key)


def revoke_agent_key(connection, schema, key_id, reason="retired"):
    """Append a revocation; the key stops working on the next call."""

    if reason not in REVOCATION_REASONS:
        raise ValueError("Unknown revocation reason")
    connection.execute(sql.SQL(
        "INSERT INTO {}.safeact_agent_key_revocations (key_id, reason) "
        "VALUES (%s, %s)"
    ).format(lsql._schema(schema)), (UUID(str(key_id)), reason))
