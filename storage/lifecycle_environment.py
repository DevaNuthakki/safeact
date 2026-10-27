"""Build one complete lifecycle schema: foundation tables to head.

Shared by the disposable test fixture and the local demo bootstrap so
both run exactly the same chain: the 0001 foundation tables (from
``storage.schema``) with their write guards, every frozen lifecycle
migration from 0003 to head, the head's role wiring, and registered
connector contracts. ``public`` is refused: it is migrated by Alembic
and stays deny-only (no registered contracts).

Statements are returned, not executed, so the caller runs them inside
one transaction it controls.
"""

import base64
import hashlib
import hmac
import secrets

import sqlalchemy as sa
from psycopg import sql
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from core.connector_profiles import LIMITS_COLUMNS, PROFILE_COLUMNS
from storage import durable_lifecycle_sql as lsql
from storage.schema import metadata


FOUNDATION_TABLES = (
    "safeact_operations",
    "safeact_attempts",
    "safeact_audit_events",
    "safeact_outbox_events",
)

SCRAM_ITERATIONS = 4096


def _non_public(schema):
    if schema == "public":
        raise ValueError("The public schema is built by Alembic only")
    return lsql._schema(schema)


def foundation_statements(schema):
    """CREATE TABLE for the 0001/0002 foundation, plus its write guards."""

    s = _non_public(schema)
    target = sa.MetaData()
    for table in metadata.sorted_tables:
        table.to_metadata(target, schema=schema)

    dialect = postgresql.dialect()
    statements = [
        sql.SQL(str(CreateTable(table).compile(dialect=dialect)).strip())
        for table in target.sorted_tables
    ]
    if {t.name for t in target.sorted_tables} != set(FOUNDATION_TABLES):
        raise RuntimeError("Foundation metadata drifted from 0001")

    statements += [
        sql.SQL(
            "CREATE TRIGGER {} BEFORE UPDATE OR DELETE "
            "ON {}.{} FOR EACH ROW "
            "EXECUTE FUNCTION public.safeact_reject_mutation()"
        ).format(
            sql.Identifier(table + "_write_guard"),
            s, sql.Identifier(table),
        )
        for table in FOUNDATION_TABLES
    ]
    return statements


def schema_statements(schema, *, owner, workers, recoverers, advancers,
                      apis=(), mcps=()):
    """Foundation, every migration to head, then the head role wiring.

    The schema itself must already exist; the caller creates it so it
    can track what it owns for cleanup.
    """

    return (
        foundation_statements(schema)
        + lsql.upgrade_statements(schema)
        + lsql.disposable_grant_statements(
            schema,
            owner=owner,
            workers=workers,
            recoverers=recoverers,
            advancers=advancers,
            apis=apis,
            mcps=mcps,
        )
    )


def contract_insert(schema):
    """Parameterized INSERT of one connector contract row."""

    return sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
        _non_public(schema),
        sql.Identifier("safeact_connector_contracts"),
        sql.SQL(", ").join(map(sql.Identifier, PROFILE_COLUMNS)),
        sql.SQL(", ").join(sql.Placeholder() for _ in PROFILE_COLUMNS),
    )


def contract_params(profile):
    row = profile.row()
    return tuple(row[c] for c in PROFILE_COLUMNS)


def limits_insert(schema):
    """Parameterized INSERT of one contract's F16 limits row (0018)."""

    return sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
        _non_public(schema),
        sql.Identifier("safeact_contract_limits"),
        sql.SQL(", ").join(map(sql.Identifier, LIMITS_COLUMNS)),
        sql.SQL(", ").join(sql.Placeholder() for _ in LIMITS_COLUMNS),
    )


def register_statements(schema, profile):
    """(statement, params) pairs registering one profile: its contract
    row and, when it declares limits, its limits row."""

    pairs = [(contract_insert(schema), contract_params(profile))]
    if profile.limits is not None:
        row = profile.limits.row(profile.connector_id, profile.version)
        pairs.append((limits_insert(schema),
                      tuple(row[c] for c in LIMITS_COLUMNS)))
    return pairs


def scram_verifier(password):
    """A SCRAM-SHA-256 verifier, so CREATE ROLE never carries plaintext.

    The server stores the verifier as given. A plaintext password in a
    CREATE ROLE statement could reach the server log under statement
    logging; the verifier is useless for logging in.
    """

    if type(password) is not str or not password.isascii() or not password:
        raise ValueError("Role passwords must be non-empty ASCII")

    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt, SCRAM_ITERATIONS
    )
    client_key = hmac.digest(salted, b"Client Key", "sha256")
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.digest(salted, b"Server Key", "sha256")

    def b64(data):
        return base64.b64encode(data).decode()

    return (
        f"SCRAM-SHA-256${SCRAM_ITERATIONS}:{b64(salt)}"
        f"${b64(stored_key)}:{b64(server_key)}"
    )
