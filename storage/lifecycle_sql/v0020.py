"""FROZEN once committed: SQL for revision 0020_metrics.

Durable gauges for Prometheus (Milestone 5, M5.7).

safeact_metrics() returns (metric, label, value) rows, computed from the
current-state projection and the 0018 tables at the moment of a scrape:

  operations                state, count
  claimable                 operations a worker may claim now
  oldest_claimable_seconds  age of the oldest of them (0 when none)
  breakers_open             provider breakers open now
  retries_waiting           RETRY_ELIGIBLE work still inside its backoff

Aggregates only: no tenant, operation, principal, key or resource ever
leaves the function. Callable by the recovery role (the reconciler
process exports it), like every other recovery function.

Pinned by tests/phase_a/test_migration_sql_frozen.py.
"""

from psycopg import sql

from storage.lifecycle_sql import v0003, v0017, v0018


REVISION = "0020_metrics"

NEW_TABLES = ()
HISTORY_TABLES = ()
NEW_INDEXES = ()
LEGAL_TRANSITIONS = frozenset()

NEW_FUNCTIONS = {"safeact_metrics": ""}
REPLACED_FUNCTIONS = ()

SHARED_FUNCTIONS = {}
WORKER_FUNCTIONS = {}
ADVANCER_FUNCTIONS = {}
RECOVERY_FUNCTIONS = {"safeact_metrics": ""}
INTERNAL_FUNCTIONS = {}
OWNER_TABLE_PRIVILEGES = {}


def _metrics(schema):
    s = v0003._schema(schema)
    claimable = v0018.claimable_query(schema, sql.SQL("1000000"))
    return sql.SQL("""CREATE FUNCTION {s}.{name}()
RETURNS TABLE(metric text, label text, value numeric) STABLE
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $safeact_lifecycle$
BEGIN
    RETURN QUERY
    SELECT 'operations'::text, p.state::text, count(*)::numeric
      FROM {s}.{p} p GROUP BY p.state;

    RETURN QUERY
    WITH c AS ({claimable})
    SELECT 'claimable'::text, ''::text, count(*)::numeric FROM c;

    RETURN QUERY
    WITH c AS ({claimable})
    SELECT 'oldest_claimable_seconds'::text, ''::text,
           coalesce(extract(epoch FROM clock_timestamp() - min(o.created_at)),
                    0)::numeric
      FROM c JOIN {s}.safeact_operations o USING (operation_id);

    RETURN QUERY
    SELECT 'breakers_open'::text, ''::text, count(*)::numeric
      FROM {s}.safeact_connector_health h
     WHERE h.open_until > clock_timestamp();

    RETURN QUERY
    SELECT 'retries_waiting'::text, ''::text, count(*)::numeric
      FROM {s}.{p} p
      JOIN {s}.safeact_retry_schedule r
        ON r.operation_id = p.operation_id AND r.seq = p.seq
     WHERE p.state = 'RETRY_ELIGIBLE' AND r.not_before > clock_timestamp();
END; $safeact_lifecycle$""").format(
        s=s, name=sql.Identifier("safeact_metrics"),
        p=sql.Identifier(v0017.PROJECTION_TABLE), claimable=claimable,
    )


def _signature(s, name, arguments):
    return sql.SQL("{}.{}({})").format(
        s, sql.Identifier(name), sql.SQL(arguments)
    )


def upgrade_statements(schema):
    s = v0003._schema(schema)
    return [_metrics(schema)] + [
        sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(
            _signature(s, name, arguments)
        )
        for name, arguments in NEW_FUNCTIONS.items()
    ]


def downgrade_statements(schema):
    s = v0003._schema(schema)
    return [
        sql.SQL("DROP FUNCTION IF EXISTS {}").format(
            _signature(s, name, arguments)
        )
        for name, arguments in NEW_FUNCTIONS.items()
    ]
