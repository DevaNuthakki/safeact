"""Head view of the durable lifecycle SQL across all migrations.

Each Alembic revision from 0003 on renders its SQL from its own frozen
module in ``storage.lifecycle_sql`` (``v0003``, ``v0004``, ...). Those
modules are never edited after commit; golden tests pin their output.

This module is not used by migrations. It composes the chain for
disposable test schemas, exposes the head's object and function
inventory, and builds the disposable-only role wiring for the head.
"""

from pathlib import Path

from psycopg import sql

from storage.lifecycle_sql import (
    v0003, v0004, v0005, v0006, v0007, v0008, v0009, v0010, v0011,
    v0012, v0013, v0014, v0015, v0016, v0017, v0018, v0019, v0020, v0021, v0022,
)
from storage.lifecycle_sql.v0003 import (  # noqa: F401
    ADMISSION_INCOMPLETE,
    CLAIM_LEASE_EXPIRED,
    CURRENT_STATE_VIEW,
    EVIDENCE_REQUIRED,
    ILLEGAL_TRANSITION,
    LEASE_NOT_EXPIRED,
    MAX_LEASE_SECONDS,
    MAX_PAGE,
    NOT_CLAIM_OWNER,
    OWNER_COLUMN_PRIVILEGES,
    RECLAIM_DISABLED,
    ROLE_PATTERN,
    STALE_TRANSITION,
    UNKNOWN_OPERATION,
    _role,
    _schema,
)
# The 0003/0007 log scans, kept for before/after measurements only.
from storage.lifecycle_sql.v0003 import (  # noqa: F401
    claimable_query as log_claimable_query,
    in_state_query as log_in_state_query,
)
from storage.lifecycle_sql.v0017 import (  # noqa: F401
    PROJECTION_DRIFT,
    PROJECTION_TABLE,
    in_state_query,
)
from storage.lifecycle_sql.v0018 import (  # noqa: F401
    NOT_YET_DUE,
    PROVIDER_LIMITED,
    RATE_LIMITED_REASON,
    claimable_query,
)
from storage.lifecycle_sql.v0004 import CONTRACT_UNKNOWN  # noqa: F401
from storage.lifecycle_sql.v0005 import (  # noqa: F401
    DEADLINE_PASSED,
    REOPEN_REFUSED,
)
from storage.lifecycle_sql.v0010 import (  # noqa: F401
    COMPENSATION_PRECONDITION,
)
from storage.lifecycle_sql.v0015 import (  # noqa: F401
    APPROVAL_EXPIRED_REASON,
)
from storage.lifecycle_sql.v0013 import (  # noqa: F401
    ALREADY_DECIDED,
    APPROVAL_REQUIRED,
    NOT_ELIGIBLE,
    SELF_REVIEW,
)
from storage.lifecycle_sql.v0012 import (  # noqa: F401
    ACTION_NOT_GRANTED,
    REQUEST_CONFLICT,
    REQUEST_INVALID,
)
from storage.lifecycle_sql.v0016 import (  # noqa: F401
    CONFLICTING_OPERATION,
)
from storage.lifecycle_sql.v0011 import (  # noqa: F401
    CSRF_REFUSED,
    IDENTITY_INVALID,
    ROLE_REQUIRED,
    UNAUTHENTICATED,
)
from storage.lifecycle_sql.v0009 import (  # noqa: F401
    STATUS_VIEW as WORKFLOW_STATUS_VIEW,
    WORKFLOW_PRECONDITION,
)
from storage.lifecycle_sql.v0007 import (  # noqa: F401
    AMBIGUOUS_REASONS as _AMBIGUOUS_0007,
    MAX_ABANDONED_CLAIMS,
    RETRY_BUDGET_EXHAUSTED,
    RETRY_NOT_PROTECTED,
    RETRY_PENDING_REASON,
)


# Applied in order. Append a module per new migration.
MIGRATIONS = (
    v0003, v0004, v0005, v0006, v0007, v0008, v0009, v0010, v0011,
    v0012, v0013, v0014, v0015, v0016, v0017, v0018, v0019, v0020, v0021, v0022,
)
LATER = MIGRATIONS[1:]

HEAD_REVISION = MIGRATIONS[-1].REVISION

# The dispatch-outcome vocabulary at head: 0007's, plus 0018's.
AMBIGUOUS_REASONS = tuple(sorted(set(_AMBIGUOUS_0007) | {RATE_LIMITED_REASON}))

ROOT = Path(__file__).resolve().parents[1]


def _merge(name):
    merged = dict(getattr(v0003, name, {}))
    for migration in LATER:
        merged.update(getattr(migration, name, {}))
    return merged


LIFECYCLE_INDEXES = v0003.LIFECYCLE_INDEXES + tuple(
    index for migration in LATER
    for index in getattr(migration, "NEW_INDEXES", ())
)
NEW_TABLES = v0003.NEW_TABLES + tuple(
    table for migration in LATER for table in migration.NEW_TABLES
)
HISTORY_TABLES = v0003.HISTORY_TABLES + tuple(
    table for migration in LATER for table in migration.HISTORY_TABLES
)

# Columns later migrations add to the foundation tables that
# storage.schema describes; autogenerate must not propose dropping them.
NEW_COLUMNS = frozenset(
    column for migration in LATER
    for column in getattr(migration, "NEW_COLUMNS", ())
)

# Views other than the 0003 current-state view (owned by the creator,
# read by humans and tests only).
NEW_VIEWS = tuple(
    view for migration in LATER
    for view in getattr(migration, "NEW_VIEWS", ())
)

WORKER_FUNCTIONS = _merge("WORKER_FUNCTIONS")
ADVANCER_FUNCTIONS = _merge("ADVANCER_FUNCTIONS")
API_FUNCTIONS = _merge("API_FUNCTIONS")
MCP_FUNCTIONS = _merge("MCP_FUNCTIONS")
RECOVERY_FUNCTIONS = _merge("RECOVERY_FUNCTIONS")
SHARED_FUNCTIONS = _merge("SHARED_FUNCTIONS")
INTERNAL_FUNCTIONS = _merge("INTERNAL_FUNCTIONS")
# Trigger functions are SECURITY DEFINER (they write on behalf of
# whichever role fired them) but no role may EXECUTE them, and
# PostgreSQL refuses to call them outside a trigger.
TRIGGER_FUNCTIONS = _merge("TRIGGER_FUNCTIONS")
PROTECTED_FUNCTIONS = {
    **WORKER_FUNCTIONS, **RECOVERY_FUNCTIONS, **SHARED_FUNCTIONS,
    **ADVANCER_FUNCTIONS, **API_FUNCTIONS, **MCP_FUNCTIONS,
}
ALL_FUNCTIONS = {
    **PROTECTED_FUNCTIONS, **INTERNAL_FUNCTIONS, **TRIGGER_FUNCTIONS,
}
DEFINER_FUNCTIONS = {**PROTECTED_FUNCTIONS, **TRIGGER_FUNCTIONS}
OWNER_TABLE_PRIVILEGES = _merge("OWNER_TABLE_PRIVILEGES")

# Every legal pair seeded by the chain. Must equal
# core.lifecycle.LEGAL_TRANSITIONS.
SEEDED_TRANSITIONS = frozenset().union(
    *(m.LEGAL_TRANSITIONS for m in MIGRATIONS)
)


def autogenerate_include_object(
    object, name, type_, reflected, compare_to
):
    """Alembic include_object hook for every lifecycle object."""

    if reflected and compare_to is None:
        if type_ == "table" and name in NEW_TABLES:
            return False
        if type_ == "index" and name in LIFECYCLE_INDEXES:
            return False
        if type_ == "column" and (object.table.name, name) in NEW_COLUMNS:
            return False
    return True


def function_signature(schema, name):
    return sql.SQL("{}.{}({})").format(
        _schema(schema),
        sql.Identifier(name),
        sql.SQL(ALL_FUNCTIONS[name]),
    )


def upgrade_statements(schema):
    """Every migration's upgrade, in order, for one schema."""

    _schema(schema)
    return [
        statement
        for migration in MIGRATIONS
        for statement in migration.upgrade_statements(schema)
    ]


def downgrade_statements(schema):
    """Every migration's downgrade, newest first."""

    _schema(schema)
    return [
        statement
        for migration in reversed(MIGRATIONS)
        for statement in migration.downgrade_statements(schema)
    ]


def disposable_grant_statements(
    schema, *, owner, workers, recoverers, advancers=(), apis=(),
    mcps=(),
):
    """Disposable-only role wiring for the head. Never used by migrations.

    Starts from the frozen 0003 wiring and adds, per later migration,
    owner privileges on its tables, ownership of its functions and
    EXECUTE for the roles that may call them.
    """

    statements = v0003.disposable_grant_statements(
        schema, owner=owner, workers=workers, recoverers=recoverers,
    )

    s = _schema(schema)
    o = _role(owner)
    workers = tuple(_role(r) for r in workers)
    recoverers = tuple(_role(r) for r in recoverers)
    advancers = tuple(_role(r) for r in advancers)
    apis = tuple(_role(r) for r in apis)
    mcps = tuple(_role(r) for r in mcps)
    if advancers + apis + mcps:
        statements.append(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            s, sql.SQL(", ").join(advancers + apis + mcps)
        ))

    def execute(names, roles):
        if not roles:
            return []
        return [
            sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(
                function_signature(schema, name),
                sql.SQL(", ").join(roles),
            )
            for name in names
        ]

    for migration in LATER:
        for table, privileges in migration.OWNER_TABLE_PRIVILEGES.items():
            statements.append(sql.SQL("GRANT {} ON {}.{} TO {}").format(
                sql.SQL(", ").join(sql.SQL(p) for p in privileges),
                s, sql.Identifier(table), o,
            ))
        statements += [
            sql.SQL("ALTER FUNCTION {} OWNER TO {}").format(
                function_signature(schema, name), o
            )
            for name in migration.NEW_FUNCTIONS
        ]
        statements += execute(migration.WORKER_FUNCTIONS, workers)
        statements += execute(migration.RECOVERY_FUNCTIONS, recoverers)
        statements += execute(
            migration.SHARED_FUNCTIONS, workers + recoverers
        )
        statements += execute(
            getattr(migration, "ADVANCER_FUNCTIONS", {}), advancers
        )
        statements += execute(getattr(migration, "API_FUNCTIONS", {}), apis)
        statements += execute(getattr(migration, "MCP_FUNCTIONS", {}), mcps)

    return statements
