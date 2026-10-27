"""FROZEN once committed: SQL for revision 0014_change_feed.

A tenant-scoped change feed for live views with durable catch-up.

- Every audit row records the transaction that wrote it
  (commit_xid xid8, defaulted from pg_current_xact_id()). Every
  existing writer is unchanged: the column fills itself.
- safeact_api_changes(secret, after) returns the operations whose audit
  rows have after <= commit_xid < xmin of the current snapshot, plus
  that xmin as the next cursor. Every transaction below the horizon has
  finished, so a row committed out of order is delayed, never skipped.
  Visibility is the operation read rule (tenant; requesters their own).
- No cursor yet (NULL) returns only the horizon: the client reads the
  current state over REST first. More changes than one page returns
  "truncated", which also means "read again". The database stays the
  source of truth; the feed only says what to re-read.

Pinned by tests/phase_a/test_migration_sql_frozen.py. Values are
literals; nothing is read from core.
"""

from psycopg import sql

from storage.lifecycle_sql import v0003, v0011, v0012


REVISION = "0014_change_feed"

FEED_PAGE = 500

NEW_TABLES = ()
HISTORY_TABLES = ()
NEW_INDEXES = ("ix_safeact_audit_events_commit_xid",)
# Columns added to tables that storage.schema describes (autogenerate
# must not propose dropping them).
NEW_COLUMNS = (("safeact_audit_events", "commit_xid"),)
LEGAL_TRANSITIONS = frozenset()

NEW_FUNCTIONS = {"safeact_api_changes": "text, text"}
REPLACED_FUNCTIONS = ()

SHARED_FUNCTIONS = {}
WORKER_FUNCTIONS = {}
RECOVERY_FUNCTIONS = {}
ADVANCER_FUNCTIONS = {}
INTERNAL_FUNCTIONS = {}
API_FUNCTIONS = {"safeact_api_changes": "text, text"}
OWNER_TABLE_PRIVILEGES = {}

_HEADER = (
    "LANGUAGE plpgsql SECURITY DEFINER "
    "SET search_path = pg_catalog, pg_temp "
    "AS $safeact_lifecycle$ "
)


def _changes(s):
    return sql.SQL(
        "CREATE FUNCTION {s}.{name}(p_secret text, p_after text) "
        "RETURNS jsonb VOLATILE " + _HEADER + """
DECLARE
    v record;
    v_horizon xid8;
    v_after xid8;
    v_changes jsonb;
    v_count integer;
BEGIN
    SELECT * INTO v
      FROM {s}.safeact_api_session(p_secret, NULL, false, NULL);
    IF cardinality(v.roles) = 0 THEN
        RAISE EXCEPTION 'Role required' USING ERRCODE = {role_required};
    END IF;

    -- Every transaction older than this has committed or aborted.
    v_horizon := pg_snapshot_xmin(pg_current_snapshot());

    IF p_after IS NULL THEN
        RETURN jsonb_build_object(
            'horizon', v_horizon::text, 'changes', '[]'::jsonb,
            'truncated', false, 'reset', true
        );
    END IF;
    IF p_after !~ '^[0-9]{{1,20}}$' THEN
        RAISE EXCEPTION 'Invalid cursor' USING ERRCODE = {invalid};
    END IF;
    v_after := p_after::xid8;
    IF v_after > v_horizon THEN
        RAISE EXCEPTION 'Invalid cursor' USING ERRCODE = {invalid};
    END IF;

    SELECT coalesce(jsonb_agg(jsonb_build_object(
               'operation_id', c.operation_id,
               'events', c.events
           ) ORDER BY c.first_xid, c.operation_id), '[]'::jsonb),
           count(*)
      INTO v_changes, v_count
      FROM (
          SELECT e.operation_id,
                 min(e.commit_xid) AS first_xid,
                 jsonb_agg(DISTINCT e.event_type) AS events
            FROM {s}.safeact_audit_events e
            JOIN {s}.safeact_operations o ON o.operation_id = e.operation_id
           WHERE e.commit_xid >= v_after
             AND e.commit_xid < v_horizon
             AND o.tenant_id = v.tenant_id
             AND (v.roles && ARRAY['investigator', 'reviewer']::text[]
                  OR {s}.safeact_api_requested_by(o.operation_id)
                     = v.principal_id::text)
           GROUP BY e.operation_id
           ORDER BY min(e.commit_xid), e.operation_id
           LIMIT {page} + 1
      ) c;

    IF v_count > {page} THEN
        -- Too much to list: tell the client to read everything again.
        RETURN jsonb_build_object(
            'horizon', v_horizon::text, 'changes', '[]'::jsonb,
            'truncated', true, 'reset', false
        );
    END IF;

    RETURN jsonb_build_object(
        'horizon', v_horizon::text, 'changes', v_changes,
        'truncated', false, 'reset', false
    );
END;
 $safeact_lifecycle$""").format(
        s=s,
        name=sql.Identifier("safeact_api_changes"),
        role_required=sql.Literal(v0011.ROLE_REQUIRED),
        invalid=sql.Literal(v0012.REQUEST_INVALID),
        page=sql.Literal(FEED_PAGE),
    )


def _signature(s):
    return sql.SQL("{}.{}(text, text)").format(
        s, sql.Identifier("safeact_api_changes")
    )


def upgrade_statements(schema):
    s = v0003._schema(schema)
    audit = sql.SQL("{}.safeact_audit_events").format(s)
    return [
        sql.SQL(
            "ALTER TABLE {} ADD COLUMN commit_xid xid8 NOT NULL "
            "DEFAULT pg_current_xact_id()"
        ).format(audit),
        sql.SQL(
            "CREATE INDEX ix_safeact_audit_events_commit_xid "
            "ON {} (commit_xid)"
        ).format(audit),
        _changes(s),
        sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(
            _signature(s)
        ),
    ]


def downgrade_statements(schema):
    s = v0003._schema(schema)
    audit = sql.SQL("{}.safeact_audit_events").format(s)
    return [
        sql.SQL("DROP FUNCTION IF EXISTS {}").format(_signature(s)),
        sql.SQL(
            "DROP INDEX IF EXISTS {}.ix_safeact_audit_events_commit_xid"
        ).format(s),
        sql.SQL("ALTER TABLE {} DROP COLUMN IF EXISTS commit_xid").format(audit),
    ]
