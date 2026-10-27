"""FROZEN once committed: SQL for revision 0022_admission_race.

Identical concurrent admissions may conflict on the derived provider key's
unique index before the tenant/request index. Handle both, then perform the
existing immutable identity and requester comparison before returning replay.
"""

from psycopg import sql

from storage.lifecycle_sql import v0003, v0012


REVISION = "0022_admission_race"
NEW_TABLES = ()
HISTORY_TABLES = ()
NEW_INDEXES = ()
LEGAL_TRANSITIONS = frozenset()
NEW_FUNCTIONS = {}
REPLACED_FUNCTIONS = ("safeact_api_admit",)
SHARED_FUNCTIONS = {}
WORKER_FUNCTIONS = {}
ADVANCER_FUNCTIONS = {}
RECOVERY_FUNCTIONS = {}
INTERNAL_FUNCTIONS = {}
OWNER_TABLE_PRIVILEGES = {}


def _replace(statement):
    text = statement.as_string(None)
    if not text.startswith("CREATE FUNCTION "):
        raise RuntimeError("0012 admission function header changed shape")
    return sql.SQL("CREATE OR REPLACE" + text[len("CREATE"):])


def upgrade_statements(schema):
    base = v0012._admit(v0003._schema(schema)).as_string(None)
    old = "ON CONFLICT ON CONSTRAINT uq_safeact_operations_tenant_request"
    if base.count(old) != 1:
        raise RuntimeError("0012 admission insert changed shape")
    return [_replace(sql.SQL(base.replace(old, "ON CONFLICT")))]


def downgrade_statements(schema):
    return [_replace(v0012._admit(v0003._schema(schema)))]
