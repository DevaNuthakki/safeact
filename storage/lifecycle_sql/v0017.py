"""FROZEN once committed: SQL for revision 0017_state_projection.

Current-state projection (Milestone 5, M5.1).

- safeact_state_projection holds one row per operation: the latest
  transition's seq, state, claim, attempt, actor, lease and time, or
  RECORDED at seq 0 before any transition. The transition log stays the
  source of truth; the projection is derived from it and can always be
  rebuilt from it.
- Written only inside the protected path, in the same transaction:
  * an AFTER INSERT trigger on safeact_operations adds the RECORDED row,
    whichever path admitted the operation;
  * safeact_lifecycle_append (the frozen 0015 append, plus one UPDATE)
    moves it to the transition it has just inserted.
- A guard trigger checks every write against the log: an insert must be
  RECORDED at seq 0 with no transitions; an update must advance exactly
  one seq and equal that transition row; a delete is refused. Anything
  else raises SAT29 (projection drift), so even the owner cannot make
  the projection say something the log does not.
- safeact_lifecycle_lock still reads the log under the row lock and also
  refuses (SAT29) when the projection's seq differs from it.
- The 0003 current-state view keeps its columns and now reads the
  projection, so every frozen reader of the view uses it unchanged. The
  claimable, relay and in-state scans, which anti-joined the log, read
  the projection's (state, operation_id) index instead.
- The upgrade locks the log against appends, backfills from it, then
  creates the guard. The downgrade restores every replaced text and
  drops the projection; nothing is lost because the log is untouched.

Pinned by tests/phase_a/test_migration_sql_frozen.py. Values are
literals; nothing is read from core.
"""

from psycopg import sql

from storage.lifecycle_sql import v0003, v0007, v0015


REVISION = "0017_state_projection"

PROJECTION_DRIFT = "SAT29"
PROJECTION_TABLE = "safeact_state_projection"

NEW_TABLES = (PROJECTION_TABLE,)
# Derived from the log, never history: dropping it loses nothing.
HISTORY_TABLES = ()
NEW_INDEXES = ("ix_safeact_state_projection_state",)
LEGAL_TRANSITIONS = frozenset()

# The seed trigger is SECURITY DEFINER: it writes the RECORDED row
# whichever role inserted the operation (in public, the admission writer,
# which holds no privilege on the projection). PostgreSQL never lets a
# trigger function be called directly, and no role may EXECUTE it.
TRIGGER_FUNCTIONS = {"safeact_state_projection_seed": ""}
NEW_FUNCTIONS = {
    "safeact_state_projection_seed": "",
    "safeact_state_projection_guard": "",
}
REPLACED_FUNCTIONS = (
    "safeact_lifecycle_lock",
    "safeact_lifecycle_append",
    "safeact_claimable_operations",
    "safeact_relay_pending",
    "safeact_operations_in_state",
)

SHARED_FUNCTIONS = {}
WORKER_FUNCTIONS = {}
RECOVERY_FUNCTIONS = {}
ADVANCER_FUNCTIONS = {}
# The guard runs as whoever writes the projection: only the owner ever
# does (inside the append, or inside the seed trigger).
INTERNAL_FUNCTIONS = {"safeact_state_projection_guard": ""}
OWNER_TABLE_PRIVILEGES = {
    PROJECTION_TABLE: ("SELECT", "INSERT", "UPDATE"),
}

_HEADER = (
    "LANGUAGE plpgsql {security} "
    "SET search_path = pg_catalog, pg_temp "
    "AS $safeact_lifecycle$ "
)


def _function(s, verb, name, args, returns, body, *, definer=True,
              volatility="VOLATILE"):
    header = (
        verb + " FUNCTION {s}.{name}(" + args + ") RETURNS "
        + returns + " " + volatility + " "
        + _HEADER.format(
            security="SECURITY DEFINER" if definer else "SECURITY INVOKER"
        )
    )
    return sql.SQL(header + body + " $safeact_lifecycle$").format(
        s=s,
        name=sql.Identifier(name),
        p=sql.Identifier(PROJECTION_TABLE),
        drift=sql.Literal(PROJECTION_DRIFT),
        unknown=sql.Literal(v0003.UNKNOWN_OPERATION),
        max_page=sql.Literal(v0003.MAX_PAGE),
        max_abandoned=sql.Literal(v0007.MAX_ABANDONED_CLAIMS),
    )


# Never-claimed work, plus RETRY_ELIGIBLE work still within the
# abandoned-claim cap. Same rows and order as the 0007 query, read from
# the projection's state index instead of anti-joining the log.
_CLAIMABLE = """
    SELECT c.operation_id FROM (
        SELECT o.operation_id, o.created_at
        FROM {s}.{p} p
        JOIN {s}.safeact_operations o
          ON o.operation_id = p.operation_id
        WHERE p.state = 'RECORDED'
        UNION ALL
        SELECT o.operation_id, o.created_at
        FROM {s}.{p} p
        JOIN {s}.safeact_operations o
          ON o.operation_id = p.operation_id
        WHERE p.state = 'RETRY_ELIGIBLE'
          AND (
              SELECT count(*) FROM {s}.safeact_operation_transitions a
              WHERE a.operation_id = p.operation_id
                AND a.from_state = 'CLAIMED'
                AND a.to_state IN ('RETRY_ELIGIBLE', 'UNKNOWN')
          ) < {max_abandoned}
    ) c
    ORDER BY c.created_at, c.operation_id
    LIMIT {limit}"""

_IN_STATE = """
    SELECT p.operation_id, p.seq
    FROM {s}.{p} p
    WHERE p.state = {state}
    ORDER BY p.operation_id
    LIMIT {limit}"""


def claimable_query(schema, limit):
    return sql.SQL(_CLAIMABLE).format(
        s=v0003._schema(schema), p=sql.Identifier(PROJECTION_TABLE),
        max_abandoned=sql.Literal(v0007.MAX_ABANDONED_CLAIMS), limit=limit,
    )


def in_state_query(schema, state, limit):
    return sql.SQL(_IN_STATE).format(
        s=v0003._schema(schema), p=sql.Identifier(PROJECTION_TABLE),
        state=state, limit=limit,
    )


def _table(s):
    # Column types copy safeact_operation_transitions, so the view keeps
    # exactly the column types it had.
    return [
        sql.SQL("""
CREATE TABLE {s}.{p} (
    operation_id uuid PRIMARY KEY
        REFERENCES {s}.safeact_operations (operation_id)
        ON DELETE RESTRICT,
    seq integer NOT NULL,
    state varchar(32) NOT NULL,
    claim_id uuid,
    attempt_id uuid,
    actor varchar(128),
    lease_expires_at timestamptz,
    state_changed_at timestamptz,
    CONSTRAINT ck_safeact_state_projection_seq CHECK (seq >= 0),
    CONSTRAINT ck_safeact_state_projection_recorded
        CHECK ((seq = 0) = (state = 'RECORDED'))
)""").format(s=s, p=sql.Identifier(PROJECTION_TABLE)),
        sql.SQL(
            "CREATE INDEX ix_safeact_state_projection_state "
            "ON {s}.{p} (state, operation_id)"
        ).format(s=s, p=sql.Identifier(PROJECTION_TABLE)),
    ]


def _backfill(s):
    # The log is locked against appends first, so no transition can
    # commit between this snapshot and the new append taking over.
    return [
        sql.SQL(
            "LOCK TABLE {s}.safeact_operation_transitions IN SHARE MODE"
        ).format(s=s),
        sql.SQL("""
INSERT INTO {s}.{p} (
    operation_id, seq, state, claim_id, attempt_id, actor,
    lease_expires_at, state_changed_at
)
SELECT o.operation_id, coalesce(t.seq, 0),
       coalesce(t.to_state, 'RECORDED'), t.claim_id, t.attempt_id,
       t.actor, t.lease_expires_at, t.created_at
FROM {s}.safeact_operations o
LEFT JOIN LATERAL (
    SELECT x.seq, x.to_state, x.claim_id, x.attempt_id, x.actor,
           x.lease_expires_at, x.created_at
    FROM {s}.safeact_operation_transitions x
    WHERE x.operation_id = o.operation_id
    ORDER BY x.seq DESC
    LIMIT 1
) t ON TRUE""").format(s=s, p=sql.Identifier(PROJECTION_TABLE)),
    ]


def _seed(s):
    return _function(
        s, "CREATE", "safeact_state_projection_seed", "", "trigger", """
BEGIN
    INSERT INTO {s}.{p} (operation_id, seq, state)
    VALUES (NEW.operation_id, 0, 'RECORDED');
    RETURN NULL;
END;""")


def _guard(s):
    return _function(
        s, "CREATE", "safeact_state_projection_guard", "", "trigger", """
DECLARE
    t record;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Projection rows are never deleted'
            USING ERRCODE = {drift};
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.seq <> 0 OR NEW.state <> 'RECORDED'
           OR NEW.claim_id IS NOT NULL OR NEW.attempt_id IS NOT NULL
           OR NEW.actor IS NOT NULL OR NEW.lease_expires_at IS NOT NULL
           OR NEW.state_changed_at IS NOT NULL
           OR EXISTS (
               SELECT 1 FROM {s}.safeact_operation_transitions x
               WHERE x.operation_id = NEW.operation_id
           ) THEN
            RAISE EXCEPTION 'Projection insert does not match the log'
                USING ERRCODE = {drift};
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.operation_id IS DISTINCT FROM OLD.operation_id
       OR NEW.seq IS DISTINCT FROM OLD.seq + 1 THEN
        RAISE EXCEPTION 'Projection must advance one transition at a time'
            USING ERRCODE = {drift};
    END IF;

    SELECT x.from_state, x.to_state, x.claim_id, x.attempt_id, x.actor,
           x.lease_expires_at, x.created_at
    INTO t
    FROM {s}.safeact_operation_transitions x
    WHERE x.operation_id = NEW.operation_id AND x.seq = NEW.seq;

    IF NOT FOUND
       OR t.from_state IS DISTINCT FROM OLD.state
       OR t.to_state IS DISTINCT FROM NEW.state
       OR t.claim_id IS DISTINCT FROM NEW.claim_id
       OR t.attempt_id IS DISTINCT FROM NEW.attempt_id
       OR t.actor IS DISTINCT FROM NEW.actor
       OR t.lease_expires_at IS DISTINCT FROM NEW.lease_expires_at
       OR t.created_at IS DISTINCT FROM NEW.state_changed_at THEN
        RAISE EXCEPTION 'Projection update does not match the log'
            USING ERRCODE = {drift};
    END IF;
    RETURN NEW;
END;""", definer=False)


def _triggers(s):
    return [
        sql.SQL(
            "CREATE TRIGGER safeact_operations_state_projection_seed "
            "AFTER INSERT ON {s}.safeact_operations FOR EACH ROW "
            "EXECUTE FUNCTION {s}.safeact_state_projection_seed()"
        ).format(s=s),
        sql.SQL(
            "CREATE TRIGGER safeact_state_projection_write_guard "
            "BEFORE INSERT OR UPDATE OR DELETE ON {s}.{p} FOR EACH ROW "
            "EXECUTE FUNCTION {s}.safeact_state_projection_guard()"
        ).format(s=s, p=sql.Identifier(PROJECTION_TABLE)),
    ]


def _lock(s):
    # The frozen 0003 lock, plus the drift check.
    return _function(
        s, "CREATE OR REPLACE", "safeact_lifecycle_lock",
        "p_operation uuid, "
        "OUT o_seq integer, OUT o_state text, OUT o_claim uuid, "
        "OUT o_attempt uuid, OUT o_actor text, "
        "OUT o_lease timestamptz",
        "record", """
DECLARE
    v_projected integer;
BEGIN
    PERFORM 1 FROM {s}.safeact_operations o
    WHERE o.operation_id = p_operation
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Unknown operation'
            USING ERRCODE = {unknown};
    END IF;

    SELECT t.seq, t.to_state, t.claim_id, t.attempt_id,
           t.actor, t.lease_expires_at
    INTO o_seq, o_state, o_claim, o_attempt, o_actor, o_lease
    FROM {s}.safeact_operation_transitions t
    WHERE t.operation_id = p_operation
    ORDER BY t.seq DESC
    LIMIT 1;

    IF NOT FOUND THEN
        o_seq := 0;
        o_state := 'RECORDED';
    END IF;

    -- 0017: the log is the truth; a projection that disagrees stops
    -- every protected write on this operation.
    SELECT p.seq INTO v_projected
    FROM {s}.{p} p
    WHERE p.operation_id = p_operation;

    IF v_projected IS DISTINCT FROM o_seq THEN
        RAISE EXCEPTION 'Current-state projection disagrees with the log'
            USING ERRCODE = {drift};
    END IF;
END;""", definer=False)


def _append(s):
    # The frozen 0015 append, plus the projection update right after the
    # transition insert. Everything else is unchanged.
    base = v0015._append(s).as_string(None)
    marker = (
        "    INSERT INTO " + s.as_string(None)
        + ".safeact_audit_events ("
    )
    if base.count(marker) != 1:
        raise RuntimeError("0015 append no longer has one audit insert")
    head, tail = base.split(marker)
    update = sql.SQL("""    -- 0017: move the current-state projection to the row just
    -- inserted. The guard trigger checks it against that row.
    UPDATE {s}.{p} p
       SET (seq, state, claim_id, attempt_id, actor, lease_expires_at,
            state_changed_at) = (
           SELECT t.seq, t.to_state, t.claim_id, t.attempt_id, t.actor,
                  t.lease_expires_at, t.created_at
           FROM {s}.safeact_operation_transitions t
           WHERE t.operation_id = p_operation AND t.seq = p_seq
       )
     WHERE p.operation_id = p_operation AND p.seq = p_seq - 1;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Current-state projection out of step'
            USING ERRCODE = {drift};
    END IF;

""").format(
        s=s, p=sql.Identifier(PROJECTION_TABLE),
        drift=sql.Literal(PROJECTION_DRIFT),
    ).as_string(None)
    return sql.SQL(head + update + marker + tail)


def _claimable_function(s):
    return _function(
        s, "CREATE OR REPLACE", "safeact_claimable_operations",
        "p_limit integer", "TABLE(operation_id uuid)", """
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND {max_page} THEN
        RAISE EXCEPTION 'Invalid page size' USING ERRCODE = '22023';
    END IF;

    RETURN QUERY """ + _CLAIMABLE.replace("{limit}", "p_limit") + """;
END;""", volatility="STABLE")


def _relay(s):
    return _function(
        s, "CREATE OR REPLACE", "safeact_relay_pending",
        "p_limit integer", "TABLE(outbox_id uuid, operation_id uuid)", """
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND {max_page} THEN
        RAISE EXCEPTION 'Invalid page size' USING ERRCODE = '22023';
    END IF;

    -- Claimable work, including reclaimable retries. The relay may
    -- publish one more than once; the claim makes that harmless.
    RETURN QUERY
    SELECT e.outbox_id, e.operation_id
    FROM (""" + _CLAIMABLE.replace("{limit}", "p_limit") + """) c
    JOIN {s}.safeact_outbox_events e
      ON e.operation_id = c.operation_id
     AND e.event_type = 'INTENT_RECORDED_NO_DISPATCH'
     AND e.state = 'PENDING';
END;""", volatility="STABLE")


def _in_state(s):
    return _function(
        s, "CREATE OR REPLACE", "safeact_operations_in_state",
        "p_state text, p_limit integer",
        "TABLE(operation_id uuid, seq integer)", """
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND {max_page}
       OR p_state IS NULL OR p_state = 'RECORDED' THEN
        RAISE EXCEPTION 'Invalid state scan' USING ERRCODE = '22023';
    END IF;

    RETURN QUERY """ + _IN_STATE.replace("{state}", "p_state").replace(
        "{limit}", "p_limit") + """;
END;""", volatility="STABLE")


def _view(s):
    # Same columns, names and types as the 0003 view; rows now come from
    # the projection.
    return sql.SQL("""
CREATE OR REPLACE VIEW {s}.{view} AS
SELECT
    o.operation_id,
    o.tenant_id,
    o.created_at,
    COALESCE(p.seq, 0) AS seq,
    COALESCE(p.state, 'RECORDED') AS state,
    p.claim_id,
    p.attempt_id,
    p.actor,
    p.lease_expires_at,
    p.state_changed_at
FROM {s}.safeact_operations o
LEFT JOIN {s}.{p} p ON p.operation_id = o.operation_id
""").format(
        s=s, view=sql.Identifier(v0003.CURRENT_STATE_VIEW),
        p=sql.Identifier(PROJECTION_TABLE),
    )


def _signature(s, name, arguments):
    return sql.SQL("{}.{}({})").format(
        s, sql.Identifier(name), sql.SQL(arguments)
    )


def _or_replace(statement):
    text = statement.as_string(None).lstrip()
    if not text.startswith("CREATE "):
        raise RuntimeError("Expected a CREATE statement")
    if text.startswith("CREATE OR REPLACE "):
        return statement
    return sql.SQL("CREATE OR REPLACE " + text[len("CREATE "):])


def upgrade_statements(schema):
    s = v0003._schema(schema)
    return (
        _table(s) + _backfill(s) + [_seed(s), _guard(s)] + _triggers(s)
        + [
            _lock(s), _append(s), _claimable_function(s), _relay(s),
            _in_state(s), _view(s),
            sql.SQL("REVOKE ALL ON TABLE {}.{} FROM PUBLIC").format(
                s, sql.Identifier(PROJECTION_TABLE)
            ),
        ]
        + [
            sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(
                _signature(s, name, arguments)
            )
            for name, arguments in NEW_FUNCTIONS.items()
        ]
    )


def downgrade_statements(schema):
    s = v0003._schema(schema)
    frozen = v0003._functions(s)
    lock, in_state = frozen[0], frozen[10]
    if "safeact_lifecycle_lock" not in lock.as_string(None) or (
        "safeact_operations_in_state" not in in_state.as_string(None)
    ):
        raise RuntimeError("0003 function order changed")
    return [
        _or_replace(v0003._view(s)),
        _or_replace(lock),
        v0015._append(s),
        v0007._claimable_function(s),
        v0007._relay(s),
        _or_replace(in_state),
        sql.SQL(
            "DROP TRIGGER IF EXISTS safeact_operations_state_projection_seed "
            "ON {s}.safeact_operations"
        ).format(s=s),
        sql.SQL("DROP TABLE IF EXISTS {s}.{p}").format(
            s=s, p=sql.Identifier(PROJECTION_TABLE)
        ),
    ] + [
        sql.SQL("DROP FUNCTION IF EXISTS {}").format(
            _signature(s, name, arguments)
        )
        for name, arguments in reversed(tuple(NEW_FUNCTIONS.items()))
    ]
