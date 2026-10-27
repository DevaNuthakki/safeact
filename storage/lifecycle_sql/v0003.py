"""FROZEN: SQL for Alembic revision 0003_durable_lifecycle.

0003 is pushed. Do not edit this module: its rendered output is pinned
byte for byte by tests/phase_a/test_migration_0003_frozen.py. Every
value that used to come from core.lifecycle, core.attempts or
core.contracts is copied here as a literal, so later changes to those
modules cannot change what 0003 creates. Schema changes belong in a
new migration module beside this one.

Append-only durable lifecycle schema and protected transitions.

One builder serves both Alembic revision 0003 (schema ``public``)
and disposable ``safeact_it_<hex32>`` test schemas.

The lifecycle is append-only. An operation's current state is its
latest ``safeact_operation_transitions`` row (none means RECORDED,
seq 0), and ``seq`` is its owner version. Existing 0001/0002 tables
and constraints are never altered, apart from one new index.

Each transition is reachable through exactly one SECURITY DEFINER
function with a fixed search_path. EXECUTE is revoked from PUBLIC.
Granting it to worker and recovery roles is a separate,
disposable-only step: no production execution role is provisioned.

Worker identity is ``session_user``; JWT-to-session binding remains
an open gate. Milestone 1 never reclaims RETRY_ELIGIBLE work.
"""

import re

from psycopg import sql


# Literal copies of the 0003-era domain values.
STATES = (
    "RECORDED", "CLAIMED", "DISPATCHING", "SUCCEEDED",
    "FAILED_CONFIRMED", "UNKNOWN", "VERIFYING", "RETRY_ELIGIBLE",
    "ESCALATED_UNRESOLVED",
)

LEGAL_TRANSITIONS = frozenset({
    ("RECORDED", "CLAIMED"),
    ("CLAIMED", "DISPATCHING"),
    ("DISPATCHING", "SUCCEEDED"),
    ("DISPATCHING", "FAILED_CONFIRMED"),
    ("DISPATCHING", "UNKNOWN"),
    ("UNKNOWN", "VERIFYING"),
    ("VERIFYING", "SUCCEEDED"),
    ("VERIFYING", "FAILED_CONFIRMED"),
    ("VERIFYING", "RETRY_ELIGIBLE"),
    ("VERIFYING", "UNKNOWN"),
    ("VERIFYING", "ESCALATED_UNRESOLVED"),
    ("RETRY_ELIGIBLE", "CLAIMED"),
})

DISPATCH_OUTCOMES = frozenset({"SUCCEEDED", "FAILED_CONFIRMED", "UNKNOWN"})

RECOVERY_TRANSITIONS = frozenset({
    ("DISPATCHING", "UNKNOWN"),
    ("UNKNOWN", "VERIFYING"),
    ("VERIFYING", "SUCCEEDED"),
    ("VERIFYING", "FAILED_CONFIRMED"),
    ("VERIFYING", "RETRY_ELIGIBLE"),
    ("VERIFYING", "UNKNOWN"),
    ("VERIFYING", "ESCALATED_UNRESOLVED"),
})

ATTEMPT_OUTCOMES = (
    "never_dispatched", "may_have_dispatched",
    "proven_not_applied", "confirmed_applied",
)

EVIDENCE_KINDS = (
    "applied", "proven_not_applied", "not_found", "inconclusive",
)


# SQLSTATEs raised by the protected functions.
ILLEGAL_TRANSITION = "SAT01"
STALE_TRANSITION = "SAT02"
NOT_CLAIM_OWNER = "SAT03"
LEASE_NOT_EXPIRED = "SAT04"
UNKNOWN_OPERATION = "SAT05"
RECLAIM_DISABLED = "SAT06"
CLAIM_LEASE_EXPIRED = "SAT07"
ADMISSION_INCOMPLETE = "SAT08"
EVIDENCE_REQUIRED = "SAT09"

MAX_LEASE_SECONDS = 3600
MAX_PAGE = 1000

NEW_TABLES = (
    "safeact_outbox_deliveries",
    "safeact_attempt_outcomes",
    "safeact_operation_transitions",
    "safeact_legal_transitions",
)

HISTORY_TABLES = (
    "safeact_operation_transitions",
    "safeact_attempt_outcomes",
    "safeact_outbox_deliveries",
)

CURRENT_STATE_VIEW = "safeact_operation_current_state"

LIFECYCLE_INDEXES = (
    "ix_safeact_operations_created_at",
    "ix_safeact_operation_transitions_state",
)


def autogenerate_include_object(
    object, name, type_, reflected, compare_to
):
    """Alembic include_object hook.

    storage.schema does not describe the 0003 objects; they are
    defined only by this builder. Hide them from autogenerate so it
    can never propose dropping them. Upgrades are unaffected.
    """

    if reflected and compare_to is None:
        if type_ == "table" and name in NEW_TABLES:
            return False
        if type_ == "index" and name in LIFECYCLE_INDEXES:
            return False
    return True

# name -> argument types. Order matters only for readability.
WORKER_FUNCTIONS = {
    "safeact_claim": "uuid, integer",
    "safeact_begin_dispatch": "uuid, integer, uuid, integer",
    "safeact_record_dispatch_outcome": (
        "uuid, integer, uuid, text, text, jsonb"
    ),
    "safeact_record_outbox_delivery": "uuid",
    "safeact_claimable_operations": "integer",
    "safeact_relay_pending": "integer",
}

RECOVERY_FUNCTIONS = {
    "safeact_recovery_transition": (
        "uuid, integer, text, text, text, jsonb"
    ),
    "safeact_operations_in_state": "text, integer",
    "safeact_recovery_snapshot": "uuid",
}

SHARED_FUNCTIONS = {
    "safeact_record_attempt_outcome": (
        "uuid, text, text, text, jsonb"
    ),
}

INTERNAL_FUNCTIONS = {
    "safeact_lifecycle_lock": "uuid",
    "safeact_lifecycle_append": (
        "uuid, integer, text, text, uuid, uuid, "
        "timestamp with time zone, text, jsonb, text"
    ),
}

PROTECTED_FUNCTIONS = {
    **WORKER_FUNCTIONS,
    **RECOVERY_FUNCTIONS,
    **SHARED_FUNCTIONS,
}

ALL_FUNCTIONS = {**PROTECTED_FUNCTIONS, **INTERNAL_FUNCTIONS}

ROLE_PATTERN = re.compile(r"safeact_[a-z0-9_]{1,55}")

# Shared by the function bodies and by EXPLAIN tests.
_CLAIMABLE_QUERY = """
    SELECT o.operation_id
    FROM {s}.safeact_operations o
    WHERE NOT EXISTS (
        SELECT 1 FROM {s}.safeact_operation_transitions t
        WHERE t.operation_id = o.operation_id
    )
    ORDER BY o.created_at, o.operation_id
    LIMIT {limit}"""

_IN_STATE_QUERY = """
    SELECT t.operation_id, t.seq
    FROM {s}.safeact_operation_transitions t
    WHERE t.to_state = {state}
      AND NOT EXISTS (
          SELECT 1 FROM {s}.safeact_operation_transitions n
          WHERE n.operation_id = t.operation_id
            AND n.seq > t.seq
      )
    ORDER BY t.operation_id
    LIMIT {limit}"""


def claimable_query(schema, limit):
    return sql.SQL(_CLAIMABLE_QUERY).format(
        s=_schema(schema), limit=limit
    )


def in_state_query(schema, state, limit):
    return sql.SQL(_IN_STATE_QUERY).format(
        s=_schema(schema), state=state, limit=limit
    )


def _schema(schema):
    if type(schema) is not str or not (
        schema == "public"
        or schema == "safeact_demo"
        or re.fullmatch(r"safeact_it_[0-9a-f]{32}", schema)
    ):
        raise ValueError(
            "Schema must be public, safeact_demo or a disposable test schema"
        )
    return sql.Identifier(schema)


def _role(role):
    if type(role) is not str or not ROLE_PATTERN.fullmatch(role):
        raise ValueError("Invalid SafeAct role name")
    return sql.Identifier(role)


def _pairs(pairs):
    return sql.SQL(", ").join(
        sql.SQL("({}, {})").format(
            sql.Literal(a), sql.Literal(b)
        )
        for a, b in sorted(pairs)
    )


def _values(values):
    return sql.SQL(", ").join(
        sql.Literal(v) for v in sorted(values)
    )


def function_signature(schema, name):
    return sql.SQL("{}.{}({})").format(
        _schema(schema),
        sql.Identifier(name),
        sql.SQL(ALL_FUNCTIONS[name]),
    )


def _tables(s):
    states = _values(STATES)
    outcomes = _values(ATTEMPT_OUTCOMES)
    evidence = _values(EVIDENCE_KINDS)

    return [
        sql.SQL("""
CREATE TABLE {s}.safeact_legal_transitions (
    from_state varchar(32) NOT NULL,
    to_state varchar(32) NOT NULL,
    PRIMARY KEY (from_state, to_state),
    CONSTRAINT ck_safeact_legal_transitions_states
        CHECK (from_state IN ({states})
           AND to_state IN ({states}))
)""").format(s=s, states=states),

        sql.SQL("""
INSERT INTO {s}.safeact_legal_transitions (from_state, to_state)
VALUES {pairs}""").format(s=s, pairs=_pairs(LEGAL_TRANSITIONS)),

        sql.SQL("""
CREATE TABLE {s}.safeact_operation_transitions (
    operation_id uuid NOT NULL
        REFERENCES {s}.safeact_operations (operation_id)
        ON DELETE RESTRICT,
    seq integer NOT NULL,
    from_state varchar(32) NOT NULL,
    to_state varchar(32) NOT NULL,
    claim_id uuid NOT NULL,
    attempt_id uuid
        REFERENCES {s}.safeact_attempts (attempt_id)
        ON DELETE RESTRICT,
    actor varchar(128) NOT NULL,
    lease_expires_at timestamptz,
    reason varchar(255) NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_safeact_operation_transitions
        PRIMARY KEY (operation_id, seq),
    CONSTRAINT fk_safeact_operation_transitions_legal
        FOREIGN KEY (from_state, to_state)
        REFERENCES {s}.safeact_legal_transitions
            (from_state, to_state),
    CONSTRAINT ck_safeact_operation_transitions_seq
        CHECK (seq > 0 AND (seq = 1) = (from_state = 'RECORDED')),
    CONSTRAINT ck_safeact_operation_transitions_text
        CHECK (actor <> '' AND reason <> ''),
    CONSTRAINT ck_safeact_operation_transitions_evidence
        CHECK (jsonb_typeof(evidence) = 'object'),
    CONSTRAINT ck_safeact_operation_transitions_fence
        CHECK (to_state <> 'DISPATCHING' OR attempt_id IS NOT NULL),
    CONSTRAINT ck_safeact_operation_transitions_lease
        CHECK (to_state NOT IN ('CLAIMED', 'DISPATCHING')
               OR lease_expires_at IS NOT NULL)
)""").format(s=s),

        sql.SQL("""
CREATE INDEX ix_safeact_operation_transitions_state
ON {s}.safeact_operation_transitions (to_state, operation_id, seq)
""").format(s=s),

        sql.SQL("""
CREATE INDEX ix_safeact_operations_created_at
ON {s}.safeact_operations (created_at, operation_id)
""").format(s=s),

        sql.SQL("""
CREATE TABLE {s}.safeact_attempt_outcomes (
    outcome_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL
        REFERENCES {s}.safeact_operations (operation_id)
        ON DELETE RESTRICT,
    attempt_id uuid NOT NULL
        REFERENCES {s}.safeact_attempts (attempt_id)
        ON DELETE RESTRICT,
    outcome varchar(32) NOT NULL,
    evidence_kind varchar(32),
    evidence_reference varchar(255),
    observed_by varchar(128) NOT NULL,
    observed_seq integer NOT NULL,
    details jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_safeact_attempt_outcomes_outcome
        CHECK (outcome IN ({outcomes})),
    CONSTRAINT ck_safeact_attempt_outcomes_evidence
        CHECK (evidence_kind IS NULL
               OR evidence_kind IN ({evidence})),
    CONSTRAINT ck_safeact_attempt_outcomes_proof
        CHECK (outcome NOT IN ('proven_not_applied',
                               'confirmed_applied')
               OR (evidence_reference IS NOT NULL
                   AND evidence_reference <> '')),
    CONSTRAINT ck_safeact_attempt_outcomes_details
        CHECK (jsonb_typeof(details) = 'object'),
    CONSTRAINT ck_safeact_attempt_outcomes_seq
        CHECK (observed_seq >= 0)
)""").format(s=s, outcomes=outcomes, evidence=evidence),

        sql.SQL("""
CREATE TABLE {s}.safeact_outbox_deliveries (
    delivery_id uuid PRIMARY KEY,
    outbox_id uuid NOT NULL
        REFERENCES {s}.safeact_outbox_events (outbox_id)
        ON DELETE RESTRICT,
    operation_id uuid NOT NULL
        REFERENCES {s}.safeact_operations (operation_id)
        ON DELETE RESTRICT,
    published_by varchar(128) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
)""").format(s=s),
    ] + [
        sql.SQL(
            "CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE "
            "ON {s}.{table} FOR EACH ROW "
            "EXECUTE FUNCTION public.safeact_reject_mutation()"
        ).format(
            s=s,
            table=sql.Identifier(table),
            trigger=sql.Identifier(table + "_write_guard"),
        )
        for table in NEW_TABLES
    ]


def _view(s):
    return sql.SQL("""
CREATE VIEW {s}.{view} AS
SELECT
    o.operation_id,
    o.tenant_id,
    o.created_at,
    COALESCE(t.seq, 0) AS seq,
    COALESCE(t.to_state, 'RECORDED') AS state,
    t.claim_id,
    t.attempt_id,
    t.actor,
    t.lease_expires_at,
    t.created_at AS state_changed_at
FROM {s}.safeact_operations o
LEFT JOIN LATERAL (
    SELECT x.seq, x.to_state, x.claim_id, x.attempt_id,
           x.actor, x.lease_expires_at, x.created_at
    FROM {s}.safeact_operation_transitions x
    WHERE x.operation_id = o.operation_id
    ORDER BY x.seq DESC
    LIMIT 1
) t ON TRUE
""").format(s=s, view=sql.Identifier(CURRENT_STATE_VIEW))


_HEADER = (
    "LANGUAGE plpgsql {security} "
    "SET search_path = pg_catalog, pg_temp "
    "AS $safeact_lifecycle$ "
)


def _function(s, name, returns, body, *, definer=True, stable=False):
    header = (
        "CREATE FUNCTION {s}.{name}({args}) RETURNS " + returns + " "
        + ("STABLE " if stable else "")
        + _HEADER.format(
            security="SECURITY DEFINER" if definer
            else "SECURITY INVOKER"
        )
    )
    return sql.SQL(
        header + body + " $safeact_lifecycle$"
    ).format(
        s=s,
        name=sql.Identifier(name),
        args=sql.SQL(_ARGUMENTS[name]),
        illegal=sql.Literal(ILLEGAL_TRANSITION),
        stale=sql.Literal(STALE_TRANSITION),
        not_owner=sql.Literal(NOT_CLAIM_OWNER),
        lease_active=sql.Literal(LEASE_NOT_EXPIRED),
        unknown=sql.Literal(UNKNOWN_OPERATION),
        reclaim=sql.Literal(RECLAIM_DISABLED),
        lease_expired=sql.Literal(CLAIM_LEASE_EXPIRED),
        incomplete=sql.Literal(ADMISSION_INCOMPLETE),
        evidence_required=sql.Literal(EVIDENCE_REQUIRED),
        max_lease=sql.Literal(MAX_LEASE_SECONDS),
        max_page=sql.Literal(MAX_PAGE),
        outcomes=_values(DISPATCH_OUTCOMES),
        recovery=_pairs(RECOVERY_TRANSITIONS),
        view=sql.Identifier(CURRENT_STATE_VIEW),
        claimable=sql.SQL(_CLAIMABLE_QUERY).format(
            s=s, limit=sql.SQL("p_limit")
        ),
        in_state=sql.SQL(_IN_STATE_QUERY).format(
            s=s, state=sql.SQL("p_state"), limit=sql.SQL("p_limit")
        ),
    )


_ARGUMENTS = {
    "safeact_lifecycle_lock": "p_operation uuid, "
    "OUT o_seq integer, OUT o_state text, OUT o_claim uuid, "
    "OUT o_attempt uuid, OUT o_actor text, "
    "OUT o_lease timestamptz",
    "safeact_lifecycle_append": (
        "p_operation uuid, p_seq integer, p_from text, p_to text, "
        "p_claim uuid, p_attempt uuid, p_lease timestamptz, "
        "p_reason text, p_evidence jsonb, p_event text"
    ),
    "safeact_claim": "p_operation uuid, p_lease_seconds integer",
    "safeact_begin_dispatch": (
        "p_operation uuid, p_expected_seq integer, p_claim uuid, "
        "p_lease_seconds integer"
    ),
    "safeact_record_dispatch_outcome": (
        "p_operation uuid, p_expected_seq integer, p_claim uuid, "
        "p_to_state text, p_reason text, p_evidence jsonb"
    ),
    "safeact_recovery_transition": (
        "p_operation uuid, p_expected_seq integer, "
        "p_expected_state text, p_to_state text, p_reason text, "
        "p_evidence jsonb"
    ),
    "safeact_record_attempt_outcome": (
        "p_attempt uuid, p_outcome text, p_evidence_kind text, "
        "p_reference text, p_details jsonb"
    ),
    "safeact_record_outbox_delivery": "p_outbox uuid",
    "safeact_claimable_operations": "p_limit integer",
    "safeact_relay_pending": "p_limit integer",
    "safeact_operations_in_state": "p_state text, p_limit integer",
    "safeact_recovery_snapshot": "p_operation uuid",
}


def _functions(s):
    lock = _function(s, "safeact_lifecycle_lock", "record", """
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
END;""", definer=False)

    append = _function(s, "safeact_lifecycle_append", "void", """
BEGIN
    IF p_reason IS NULL OR length(p_reason) NOT BETWEEN 1 AND 255
       OR p_evidence IS NULL
       OR jsonb_typeof(p_evidence) <> 'object' THEN
        RAISE EXCEPTION 'Invalid transition reason or evidence'
            USING ERRCODE = '22023';
    END IF;

    PERFORM 1 FROM {s}.safeact_legal_transitions l
    WHERE l.from_state = p_from AND l.to_state = p_to;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Illegal transition % -> %', p_from, p_to
            USING ERRCODE = {illegal};
    END IF;

    -- Outcome claims need recorded proof, on every path. A timeout,
    -- empty lookup, NOT_FOUND or INCONCLUSIVE result never proves
    -- non-application, so it can never reach FAILED_CONFIRMED.
    IF p_to = 'FAILED_CONFIRMED' AND (
        NOT EXISTS (
            SELECT 1 FROM {s}.safeact_attempts a
            WHERE a.operation_id = p_operation
        )
        OR EXISTS (
            SELECT 1 FROM {s}.safeact_attempts a
            WHERE a.operation_id = p_operation
              AND NOT EXISTS (
                  SELECT 1 FROM {s}.safeact_attempt_outcomes x
                  WHERE x.attempt_id = a.attempt_id
                    AND x.outcome = 'proven_not_applied'
                    AND x.evidence_kind = 'proven_not_applied'
                    AND x.evidence_reference IS NOT NULL
              )
        )
        OR EXISTS (
            SELECT 1 FROM {s}.safeact_attempt_outcomes x
            WHERE x.operation_id = p_operation
              AND (x.outcome = 'confirmed_applied'
                   OR x.evidence_kind = 'applied')
        )
    ) THEN
        RAISE EXCEPTION
            'Authoritative non-application evidence required'
            USING ERRCODE = {evidence_required};
    END IF;

    IF p_to = 'SUCCEEDED' AND NOT EXISTS (
        SELECT 1 FROM {s}.safeact_attempt_outcomes x
        WHERE x.operation_id = p_operation
          AND x.outcome = 'confirmed_applied'
          AND x.evidence_kind = 'applied'
          AND x.evidence_reference IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'Applied-effect evidence required'
            USING ERRCODE = {evidence_required};
    END IF;

    INSERT INTO {s}.safeact_operation_transitions (
        operation_id, seq, from_state, to_state, claim_id,
        attempt_id, actor, lease_expires_at, reason, evidence
    ) VALUES (
        p_operation, p_seq, p_from, p_to, p_claim, p_attempt,
        session_user::text, p_lease, p_reason, p_evidence
    );

    INSERT INTO {s}.safeact_audit_events (
        operation_id, event_type, actor_id, details
    ) VALUES (
        p_operation, p_event, session_user::text,
        jsonb_build_object(
            'schema_version', 1,
            'seq', p_seq,
            'from_state', p_from,
            'to_state', p_to,
            'claim_id', p_claim,
            'attempt_id', p_attempt,
            'reason', p_reason
        )
    );
END;""", definer=False)

    claim = _function(
        s, "safeact_claim",
        "TABLE(claim_id uuid, seq integer, tenant_id text, "
        "action_type text, resource_id text, provider_account_id text, "
        "connector_id text, contract_version text, payload_sha256 text, "
        "payload_canonical text, provider_idempotency_key text)", """
DECLARE
    v record;
    v_claim uuid := gen_random_uuid();
BEGIN
    IF p_lease_seconds IS NULL
       OR p_lease_seconds NOT BETWEEN 1 AND {max_lease} THEN
        RAISE EXCEPTION 'Invalid lease' USING ERRCODE = '22023';
    END IF;

    SELECT * INTO v FROM {s}.safeact_lifecycle_lock(p_operation);

    -- Milestone 1 claims only never-claimed RECORDED work.
    IF v.o_state = 'RETRY_ELIGIBLE' THEN
        RAISE EXCEPTION 'Reclaim is disabled in Milestone 1'
            USING ERRCODE = {reclaim};
    END IF;

    IF v.o_seq <> 0 THEN
        RAISE EXCEPTION 'Operation already claimed'
            USING ERRCODE = {stale};
    END IF;

    PERFORM 1 FROM {s}.safeact_outbox_events e
    WHERE e.operation_id = p_operation
      AND e.event_type = 'INTENT_RECORDED_NO_DISPATCH'
      AND e.state = 'PENDING';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Admission bundle incomplete'
            USING ERRCODE = {incomplete};
    END IF;

    PERFORM {s}.safeact_lifecycle_append(
        p_operation, 1, 'RECORDED', 'CLAIMED', v_claim, NULL,
        clock_timestamp() + make_interval(secs => p_lease_seconds),
        'claimed', '{{}}'::jsonb, 'LIFECYCLE_CLAIMED'
    );

    -- Dispatch inputs come from the immutable durable record read
    -- under this lock, never from the delivered task.
    RETURN QUERY
    SELECT v_claim, 1,
           o.tenant_id::text, o.action_type::text,
           o.resource_id::text, o.provider_account_id::text,
           o.connector_id::text, o.contract_version::text,
           o.payload_sha256::text, o.payload_canonical,
           o.provider_idempotency_key::text
    FROM {s}.safeact_operations o
    WHERE o.operation_id = p_operation;
END;""")

    begin_dispatch = _function(
        s, "safeact_begin_dispatch",
        "TABLE(seq integer, attempt_id uuid)", """
DECLARE
    v record;
    v_attempt uuid := gen_random_uuid();
    v_ordinal integer;
BEGIN
    IF p_lease_seconds IS NULL
       OR p_lease_seconds NOT BETWEEN 1 AND {max_lease} THEN
        RAISE EXCEPTION 'Invalid lease' USING ERRCODE = '22023';
    END IF;

    SELECT * INTO v FROM {s}.safeact_lifecycle_lock(p_operation);

    IF v.o_seq IS DISTINCT FROM p_expected_seq THEN
        RAISE EXCEPTION 'Stale owner version'
            USING ERRCODE = {stale};
    END IF;

    IF v.o_state <> 'CLAIMED' THEN
        RAISE EXCEPTION 'Illegal transition % -> DISPATCHING',
            v.o_state USING ERRCODE = {illegal};
    END IF;

    IF v.o_claim IS DISTINCT FROM p_claim
       OR v.o_actor IS DISTINCT FROM session_user::text THEN
        RAISE EXCEPTION 'Not the claim owner'
            USING ERRCODE = {not_owner};
    END IF;

    IF v.o_lease <= clock_timestamp() THEN
        RAISE EXCEPTION 'Claim lease expired before dispatch'
            USING ERRCODE = {lease_expired};
    END IF;

    SELECT count(*) + 1 INTO v_ordinal
    FROM {s}.safeact_attempts a
    WHERE a.operation_id = p_operation;

    INSERT INTO {s}.safeact_attempts (
        attempt_id, operation_id, ordinal, dispatch_risk
    ) VALUES (v_attempt, p_operation, v_ordinal, 'PREPARED');

    -- Committing this row is the dispatch-risk fence: from here
    -- the provider call may have happened.
    PERFORM {s}.safeact_lifecycle_append(
        p_operation, v.o_seq + 1, 'CLAIMED', 'DISPATCHING',
        p_claim, v_attempt,
        clock_timestamp() + make_interval(secs => p_lease_seconds),
        'dispatch_risk_committed', '{{}}'::jsonb,
        'DISPATCH_RISK_COMMITTED'
    );

    RETURN QUERY SELECT v.o_seq + 1, v_attempt;
END;""")

    outcome = _function(
        s, "safeact_record_dispatch_outcome", "integer", """
DECLARE
    v record;
BEGIN
    SELECT * INTO v FROM {s}.safeact_lifecycle_lock(p_operation);

    IF v.o_seq IS DISTINCT FROM p_expected_seq THEN
        RAISE EXCEPTION 'Stale owner version'
            USING ERRCODE = {stale};
    END IF;

    IF v.o_state <> 'DISPATCHING'
       OR p_to_state IS NULL
       OR p_to_state NOT IN ({outcomes}) THEN
        RAISE EXCEPTION 'Illegal transition % -> %',
            v.o_state, p_to_state USING ERRCODE = {illegal};
    END IF;

    IF v.o_claim IS DISTINCT FROM p_claim
       OR v.o_actor IS DISTINCT FROM session_user::text THEN
        RAISE EXCEPTION 'Not the claim owner'
            USING ERRCODE = {not_owner};
    END IF;

    PERFORM {s}.safeact_lifecycle_append(
        p_operation, v.o_seq + 1, 'DISPATCHING', p_to_state,
        v.o_claim, v.o_attempt, NULL, p_reason, p_evidence,
        'LIFECYCLE_DISPATCH_OUTCOME'
    );

    RETURN v.o_seq + 1;
END;""")

    recovery = _function(
        s, "safeact_recovery_transition", "integer", """
DECLARE
    v record;
BEGIN
    SELECT * INTO v FROM {s}.safeact_lifecycle_lock(p_operation);

    IF v.o_seq IS DISTINCT FROM p_expected_seq
       OR v.o_state IS DISTINCT FROM p_expected_state THEN
        RAISE EXCEPTION 'Stale owner version or state'
            USING ERRCODE = {stale};
    END IF;

    IF (p_expected_state, p_to_state) NOT IN ({recovery}) THEN
        RAISE EXCEPTION 'Illegal recovery transition % -> %',
            p_expected_state, p_to_state USING ERRCODE = {illegal};
    END IF;

    -- An expired lease permits investigation, never dispatch.
    IF p_expected_state = 'DISPATCHING'
       AND v.o_lease > clock_timestamp() THEN
        RAISE EXCEPTION 'Dispatch lease has not expired'
            USING ERRCODE = {lease_active};
    END IF;

    PERFORM {s}.safeact_lifecycle_append(
        p_operation, v.o_seq + 1, p_expected_state, p_to_state,
        v.o_claim, v.o_attempt, NULL, p_reason, p_evidence,
        'LIFECYCLE_RECOVERY'
    );

    RETURN v.o_seq + 1;
END;""")

    attempt_outcome = _function(
        s, "safeact_record_attempt_outcome", "uuid", """
DECLARE
    v record;
    v_operation uuid;
    v_id uuid := gen_random_uuid();
BEGIN
    SELECT a.operation_id INTO v_operation
    FROM {s}.safeact_attempts a
    WHERE a.attempt_id = p_attempt;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Unknown attempt'
            USING ERRCODE = {unknown};
    END IF;

    -- Evidence is appended, never replacing earlier evidence or
    -- the current decision; observed_seq records what it saw.
    SELECT * INTO v FROM {s}.safeact_lifecycle_lock(v_operation);

    INSERT INTO {s}.safeact_attempt_outcomes (
        outcome_id, operation_id, attempt_id, outcome,
        evidence_kind, evidence_reference, observed_by,
        observed_seq, details
    ) VALUES (
        v_id, v_operation, p_attempt, p_outcome, p_evidence_kind,
        p_reference, session_user::text, v.o_seq,
        COALESCE(p_details, '{{}}'::jsonb)
    );

    INSERT INTO {s}.safeact_audit_events (
        operation_id, event_type, actor_id, details
    ) VALUES (
        v_operation, 'ATTEMPT_EVIDENCE_RECORDED', session_user::text,
        jsonb_build_object(
            'schema_version', 1,
            'outcome_id', v_id,
            'attempt_id', p_attempt,
            'outcome', p_outcome,
            'observed_seq', v.o_seq
        )
    );

    RETURN v_id;
END;""")

    delivery = _function(
        s, "safeact_record_outbox_delivery", "uuid", """
DECLARE
    v_operation uuid;
    v_id uuid := gen_random_uuid();
BEGIN
    SELECT e.operation_id INTO v_operation
    FROM {s}.safeact_outbox_events e
    WHERE e.outbox_id = p_outbox;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Unknown outbox event'
            USING ERRCODE = {unknown};
    END IF;

    -- Deliveries are at-least-once: many rows per event allowed.
    INSERT INTO {s}.safeact_outbox_deliveries (
        delivery_id, outbox_id, operation_id, published_by
    ) VALUES (v_id, p_outbox, v_operation, session_user::text);

    RETURN v_id;
END;""")

    claimable = _function(
        s, "safeact_claimable_operations",
        "TABLE(operation_id uuid)", """
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND {max_page} THEN
        RAISE EXCEPTION 'Invalid page size' USING ERRCODE = '22023';
    END IF;

    -- Never-claimed work only: RETRY_ELIGIBLE is not claimable
    -- in Milestone 1.
    RETURN QUERY {claimable};
END;""", stable=True)

    relay = _function(
        s, "safeact_relay_pending",
        "TABLE(outbox_id uuid, operation_id uuid)", """
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND {max_page} THEN
        RAISE EXCEPTION 'Invalid page size' USING ERRCODE = '22023';
    END IF;

    -- Outbox events whose operation was never claimed. The relay
    -- may publish one more than once; workers tolerate duplicates.
    RETURN QUERY
    SELECT e.outbox_id, e.operation_id
    FROM ({claimable}) c
    JOIN {s}.safeact_outbox_events e
      ON e.operation_id = c.operation_id
     AND e.event_type = 'INTENT_RECORDED_NO_DISPATCH'
     AND e.state = 'PENDING';
END;""", stable=True)

    snapshot = _function(
        s, "safeact_recovery_snapshot",
        "TABLE(seq integer, state text, tenant_id text, "
        "action_type text, resource_id text, provider_account_id text, "
        "connector_id text, contract_version text, payload_sha256 text, "
        "provider_idempotency_key text, first_dispatch_at timestamptz, "
        "lease_expires_at timestamptz, observed_at timestamptz, "
        "attempts jsonb)", """
BEGIN
    -- Read-only inputs for the pure recovery policy. Attempt
    -- inventory is complete by construction: attempts are created
    -- only by safeact_begin_dispatch.
    RETURN QUERY
    SELECT c.seq, c.state::text,
           o.tenant_id::text, o.action_type::text,
           o.resource_id::text, o.provider_account_id::text,
           o.connector_id::text, o.contract_version::text,
           o.payload_sha256::text, o.provider_idempotency_key::text,
           (SELECT min(t.created_at)
            FROM {s}.safeact_operation_transitions t
            WHERE t.operation_id = o.operation_id
              AND t.to_state = 'DISPATCHING'),
           c.lease_expires_at,
           clock_timestamp(),
           COALESCE((
               SELECT jsonb_agg(jsonb_build_object(
                   'attempt_id', a.attempt_id,
                   'ordinal', a.ordinal,
                   'applied_reference', (
                       SELECT max(x.evidence_reference)
                       FROM {s}.safeact_attempt_outcomes x
                       WHERE x.attempt_id = a.attempt_id
                         AND x.outcome = 'confirmed_applied'
                         AND x.evidence_kind = 'applied'),
                   'not_applied_reference', (
                       SELECT max(x.evidence_reference)
                       FROM {s}.safeact_attempt_outcomes x
                       WHERE x.attempt_id = a.attempt_id
                         AND x.outcome = 'proven_not_applied'
                         AND x.evidence_kind = 'proven_not_applied')
               ) ORDER BY a.ordinal)
               FROM {s}.safeact_attempts a
               WHERE a.operation_id = o.operation_id
           ), '[]'::jsonb)
    FROM {s}.safeact_operations o
    JOIN {s}.{view} c ON c.operation_id = o.operation_id
    WHERE o.operation_id = p_operation;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Unknown operation'
            USING ERRCODE = {unknown};
    END IF;
END;""", stable=True)

    in_state = _function(
        s, "safeact_operations_in_state",
        "TABLE(operation_id uuid, seq integer)", """
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND {max_page}
       OR p_state IS NULL OR p_state = 'RECORDED' THEN
        RAISE EXCEPTION 'Invalid state scan' USING ERRCODE = '22023';
    END IF;

    RETURN QUERY {in_state};
END;""", stable=True)

    return [
        lock, append, claim, begin_dispatch, outcome, recovery,
        attempt_outcome, delivery, claimable, relay, in_state,
        snapshot,
    ]


def upgrade_statements(schema):
    s = _schema(schema)

    statements = _tables(s) + [_view(s)] + _functions(s)

    # Nobody, including PUBLIC, may call these until a reviewed,
    # disposable-only grant step names specific roles.
    statements += [
        sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(
            function_signature(schema, name)
        )
        for name in ALL_FUNCTIONS
    ]
    return statements


def downgrade_statements(schema):
    s = _schema(schema)

    return [
        sql.SQL("DROP FUNCTION IF EXISTS {}").format(
            function_signature(schema, name)
        )
        for name in reversed(tuple(ALL_FUNCTIONS))
    ] + [
        sql.SQL("DROP VIEW IF EXISTS {s}.{view}").format(
            s=s, view=sql.Identifier(CURRENT_STATE_VIEW)
        ),
        sql.SQL(
            "DROP INDEX IF EXISTS {s}.ix_safeact_operations_created_at"
        ).format(s=s),
    ] + [
        sql.SQL("DROP TABLE IF EXISTS {s}.{table}").format(
            s=s, table=sql.Identifier(table)
        )
        for table in NEW_TABLES
    ]


# Minimum privileges the function bodies need, as the owner.
OWNER_TABLE_PRIVILEGES = {
    "safeact_operations": ("SELECT",),
    "safeact_attempts": ("SELECT", "INSERT"),
    "safeact_audit_events": ("INSERT",),
    "safeact_outbox_events": ("SELECT",),
    "safeact_legal_transitions": ("SELECT",),
    "safeact_operation_transitions": ("SELECT", "INSERT"),
    "safeact_attempt_outcomes": ("SELECT", "INSERT"),
    "safeact_outbox_deliveries": ("INSERT",),
}

# PostgreSQL requires UPDATE on at least one column for
# SELECT ... FOR UPDATE. Real UPDATEs stay blocked by the
# existing safeact_reject_mutation trigger.
OWNER_COLUMN_PRIVILEGES = {
    ("safeact_operations", "operation_id"): ("UPDATE",),
}


def disposable_grant_statements(
    schema, *, owner, workers, recoverers
):
    """Disposable-only role wiring. Never used by migrations."""

    if schema == "public":
        raise ValueError("Grants are disposable-schema only")

    s = _schema(schema)
    o = _role(owner)
    workers = tuple(_role(r) for r in workers)
    recoverers = tuple(_role(r) for r in recoverers)
    callers = workers + recoverers

    statements = [
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            s, sql.SQL(", ").join((o,) + callers)
        ),
    ]

    for table, privileges in OWNER_TABLE_PRIVILEGES.items():
        statements.append(sql.SQL("GRANT {} ON {}.{} TO {}").format(
            sql.SQL(", ").join(sql.SQL(p) for p in privileges),
            s, sql.Identifier(table), o,
        ))

    for (table, column), privileges in (
        OWNER_COLUMN_PRIVILEGES.items()
    ):
        statements.append(
            sql.SQL("GRANT {} ({}) ON {}.{} TO {}").format(
                sql.SQL(", ").join(sql.SQL(p) for p in privileges),
                sql.Identifier(column),
                s, sql.Identifier(table), o,
            )
        )

    statements += [
        sql.SQL("ALTER FUNCTION {} OWNER TO {}").format(
            function_signature(schema, name), o
        )
        for name in ALL_FUNCTIONS
    ]
    view = sql.SQL("{}.{}").format(
        s, sql.Identifier(CURRENT_STATE_VIEW)
    )
    statements += [
        sql.SQL("ALTER VIEW {} OWNER TO {}").format(view, o),
        # The view is read-only: drop the owner's implicit write
        # privileges so its ACL holds SELECT only.
        sql.SQL("REVOKE ALL ON {} FROM {}").format(view, o),
        sql.SQL("GRANT SELECT ON {} TO {}").format(view, o),
    ]

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

    statements += execute(WORKER_FUNCTIONS, workers)
    statements += execute(RECOVERY_FUNCTIONS, recoverers)
    statements += execute(SHARED_FUNCTIONS, callers)

    if callers:
        statements.append(sql.SQL("GRANT SELECT ON {}.{} TO {}").format(
            s, sql.Identifier(CURRENT_STATE_VIEW),
            sql.SQL(", ").join(callers),
        ))

    return statements
