"""FROZEN once committed: SQL for revision 0006_claim_exit.

A safe way out of CLAIMED after a crash. Nothing is dispatched while
CLAIMED (the fence creates the attempt and DISPATCHING atomically), so
an abandoned claim can leave CLAIMED without risk:

- CLAIMED -> RETRY_ELIGIBLE when the operation has no attempt at all;
- CLAIMED -> UNKNOWN when earlier attempts exist (re-verify, never a
  blind retry).

The database chooses the exit from the attempt history (SAT01 for the
wrong one). The claim owner may release at once
(safeact_release_claim); the recovery role only after the claim lease
expired (SAT04). A stale claimant that resumes afterwards is refused at
the fence because state, seq and claim no longer match.

Pinned by tests/phase_a/test_migration_sql_frozen.py. Values are
literals; nothing is read from core.
"""

from psycopg import sql

from storage.lifecycle_sql import v0003, v0005


REVISION = "0006_claim_exit"

NEW_TABLES = ()
HISTORY_TABLES = ()
LEGAL_TRANSITIONS = frozenset({
    ("CLAIMED", "RETRY_ELIGIBLE"),
    ("CLAIMED", "UNKNOWN"),
})

RECOVERY_TRANSITIONS = frozenset({
    ("DISPATCHING", "UNKNOWN"),
    ("UNKNOWN", "VERIFYING"),
    ("VERIFYING", "SUCCEEDED"),
    ("VERIFYING", "FAILED_CONFIRMED"),
    ("VERIFYING", "RETRY_ELIGIBLE"),
    ("VERIFYING", "UNKNOWN"),
    ("VERIFYING", "ESCALATED_UNRESOLVED"),
    ("ESCALATED_UNRESOLVED", "VERIFYING"),
    ("CLAIMED", "RETRY_ELIGIBLE"),
    ("CLAIMED", "UNKNOWN"),
})

NEW_FUNCTIONS = {
    "safeact_release_claim": "uuid, integer, uuid, text",
}
REPLACED_FUNCTIONS = (
    "safeact_lifecycle_append",
    "safeact_recovery_transition",
)

SHARED_FUNCTIONS = {}
WORKER_FUNCTIONS = {"safeact_release_claim": "uuid, integer, uuid, text"}
RECOVERY_FUNCTIONS = {}
INTERNAL_FUNCTIONS = {}
OWNER_TABLE_PRIVILEGES = {}

_HEADER = (
    "LANGUAGE plpgsql {security} "
    "SET search_path = pg_catalog, pg_temp "
    "AS $safeact_lifecycle$ "
)


def _pairs(pairs):
    return sql.SQL(", ").join(
        sql.SQL("({}, {})").format(sql.Literal(a), sql.Literal(b))
        for a, b in sorted(pairs)
    )


def _function(s, name, args, returns, body, *, verb, definer=True,
              stable=False):
    header = (
        verb + " FUNCTION {s}.{name}(" + args + ") RETURNS "
        + returns + " " + ("STABLE " if stable else "")
        + _HEADER.format(
            security="SECURITY DEFINER" if definer else "SECURITY INVOKER"
        )
    )
    return sql.SQL(header + body + " $safeact_lifecycle$").format(
        s=s,
        name=sql.Identifier(name),
        illegal=sql.Literal(v0003.ILLEGAL_TRANSITION),
        stale=sql.Literal(v0003.STALE_TRANSITION),
        not_owner=sql.Literal(v0003.NOT_CLAIM_OWNER),
        lease_active=sql.Literal(v0003.LEASE_NOT_EXPIRED),
        evidence_required=sql.Literal(v0003.EVIDENCE_REQUIRED),
        deadline_passed=sql.Literal(v0005.DEADLINE_PASSED),
        reopen_refused=sql.Literal(v0005.REOPEN_REFUSED),
        recovery=_pairs(RECOVERY_TRANSITIONS),
    )


def _append(s):
    return _function(
        s, "safeact_lifecycle_append",
        "p_operation uuid, p_seq integer, p_from text, p_to text, "
        "p_claim uuid, p_attempt uuid, p_lease timestamptz, "
        "p_reason text, p_evidence jsonb, p_event text",
        "void", """
DECLARE
    v_deadline timestamptz;
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

    -- After the investigation deadline there is no "verify later":
    -- resolve with evidence or escalate. The deadline notifies; it
    -- never turns absence into proof.
    IF p_from = 'VERIFYING' AND p_to = 'UNKNOWN' THEN
        v_deadline := {s}.safeact_investigation_deadline(p_operation);
        IF v_deadline IS NULL OR clock_timestamp() >= v_deadline THEN
            RAISE EXCEPTION 'Investigation deadline has passed'
                USING ERRCODE = {deadline_passed};
        END IF;
    END IF;

    -- Reopen only on decisive evidence recorded after this escalation
    -- and observed at its seq: one row justifies at most one reopen.
    IF p_from = 'ESCALATED_UNRESOLVED' AND p_to = 'VERIFYING' THEN
        IF NOT EXISTS (
            SELECT 1
            FROM {s}.safeact_attempt_outcomes x
            JOIN {s}.safeact_operation_transitions e
              ON e.operation_id = x.operation_id
             AND e.seq = p_seq - 1
             AND e.to_state = 'ESCALATED_UNRESOLVED'
            WHERE x.operation_id = p_operation
              AND x.observed_seq = p_seq - 1
              AND x.created_at > e.created_at
              AND x.evidence_reference IS NOT NULL
              AND ((x.outcome = 'confirmed_applied'
                    AND x.evidence_kind = 'applied')
                OR (x.outcome = 'proven_not_applied'
                    AND x.evidence_kind = 'proven_not_applied'))
        ) THEN
            RAISE EXCEPTION 'New decisive evidence required to reopen'
                USING ERRCODE = {evidence_required};
        END IF;

        IF (
            SELECT count(*)
            FROM {s}.safeact_operation_transitions t
            WHERE t.operation_id = p_operation
              AND t.from_state = 'ESCALATED_UNRESOLVED'
              AND t.to_state = 'VERIFYING'
        ) >= COALESCE((
            SELECT c.max_reopens
            FROM {s}.safeact_operations o
            JOIN {s}.safeact_connector_contracts c
              ON c.connector_id = o.connector_id
             AND c.version = o.contract_version
            WHERE o.operation_id = p_operation
        ), 0) THEN
            RAISE EXCEPTION 'Reopen budget exhausted'
                USING ERRCODE = {reopen_refused};
        END IF;
    END IF;

    -- An abandoned claim dispatched nothing under it. With no attempt
    -- ever made the operation may be claimed again; with earlier
    -- attempts it must be verified again, never retried blindly. The
    -- database picks the exit from the attempt history.
    IF p_from = 'CLAIMED' AND p_to IN ('RETRY_ELIGIBLE', 'UNKNOWN') THEN
        IF (p_to = 'RETRY_ELIGIBLE') = EXISTS (
            SELECT 1 FROM {s}.safeact_attempts a
            WHERE a.operation_id = p_operation
        ) THEN
            RAISE EXCEPTION
                'Abandoned claim: % is not the exit for this history',
                p_to USING ERRCODE = {illegal};
        END IF;
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
END;""", verb="CREATE OR REPLACE", definer=False)


def _recovery(s):
    return _function(
        s, "safeact_recovery_transition",
        "p_operation uuid, p_expected_seq integer, "
        "p_expected_state text, p_to_state text, p_reason text, "
        "p_evidence jsonb",
        "integer", """
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

    -- An expired lease permits investigation or an abandoned-claim
    -- exit, never dispatch. A live claim or dispatch is left alone.
    IF p_expected_state IN ('CLAIMED', 'DISPATCHING')
       AND v.o_lease > clock_timestamp() THEN
        RAISE EXCEPTION 'Lease has not expired'
            USING ERRCODE = {lease_active};
    END IF;

    PERFORM {s}.safeact_lifecycle_append(
        p_operation, v.o_seq + 1, p_expected_state, p_to_state,
        v.o_claim, v.o_attempt, NULL, p_reason, p_evidence,
        'LIFECYCLE_RECOVERY'
    );

    RETURN v.o_seq + 1;
END;""", verb="CREATE OR REPLACE")


def _release(s):
    return _function(
        s, "safeact_release_claim",
        "p_operation uuid, p_expected_seq integer, p_claim uuid, "
        "p_reason text",
        "TABLE(seq integer, state text)", """
DECLARE
    v record;
    v_to text;
BEGIN
    SELECT * INTO v FROM {s}.safeact_lifecycle_lock(p_operation);

    IF v.o_seq IS DISTINCT FROM p_expected_seq THEN
        RAISE EXCEPTION 'Stale owner version'
            USING ERRCODE = {stale};
    END IF;

    IF v.o_state <> 'CLAIMED' THEN
        RAISE EXCEPTION 'Illegal transition % -> release', v.o_state
            USING ERRCODE = {illegal};
    END IF;

    IF v.o_claim IS DISTINCT FROM p_claim
       OR v.o_actor IS DISTINCT FROM session_user::text THEN
        RAISE EXCEPTION 'Not the claim owner'
            USING ERRCODE = {not_owner};
    END IF;

    -- The owner gives the claim up before any dispatch. The exit
    -- depends only on the durable attempt history.
    v_to := CASE WHEN EXISTS (
        SELECT 1 FROM {s}.safeact_attempts a
        WHERE a.operation_id = p_operation
    ) THEN 'UNKNOWN' ELSE 'RETRY_ELIGIBLE' END;

    PERFORM {s}.safeact_lifecycle_append(
        p_operation, v.o_seq + 1, 'CLAIMED', v_to, v.o_claim, NULL,
        NULL, p_reason, '{{}}'::jsonb, 'LIFECYCLE_CLAIM_RELEASED'
    );

    RETURN QUERY SELECT v.o_seq + 1, v_to;
END;""", verb="CREATE")


def _signature(s, name, arguments):
    return sql.SQL("{}.{}({})").format(
        s, sql.Identifier(name), sql.SQL(arguments)
    )


def _seed(s, pairs):
    return sql.SQL(
        "INSERT INTO {s}.safeact_legal_transitions (from_state, to_state) "
        "VALUES {pairs}"
    ).format(s=s, pairs=_pairs(pairs))


def upgrade_statements(schema):
    s = v0003._schema(schema)
    return [
        _seed(s, LEGAL_TRANSITIONS),
        _append(s),
        _recovery(s),
        _release(s),
    ] + [
        sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(
            _signature(s, name, arguments)
        )
        for name, arguments in NEW_FUNCTIONS.items()
    ]


def downgrade_statements(schema):
    s = v0003._schema(schema)
    # 0005's definitions are already CREATE OR REPLACE.
    previous = {
        "safeact_lifecycle_append": v0005._append(s),
        "safeact_recovery_transition": v0005._recovery(s),
    }
    guard = sql.Identifier("safeact_legal_transitions_write_guard")
    table = sql.SQL("{}.{}").format(
        s, sql.Identifier("safeact_legal_transitions")
    )
    return [
        previous[name] for name in REPLACED_FUNCTIONS
    ] + [
        sql.SQL("DROP FUNCTION IF EXISTS {}").format(
            _signature(s, name, arguments)
        )
        for name, arguments in NEW_FUNCTIONS.items()
    ] + [
        sql.SQL("ALTER TABLE {} DISABLE TRIGGER {}").format(table, guard),
        sql.SQL(
            "DELETE FROM {} WHERE (from_state, to_state) IN ({})"
        ).format(table, _pairs(LEGAL_TRANSITIONS)),
        sql.SQL("ALTER TABLE {} ENABLE TRIGGER {}").format(table, guard),
    ]
