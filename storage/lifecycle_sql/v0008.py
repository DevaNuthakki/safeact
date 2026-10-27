"""FROZEN once committed: SQL for revision 0008_keys_escalation.

Hardening after the C5 review.

- Provider keys are derived server-side from operation identity. A CHECK
  recomputes the derivation ('sa1_' + sha256 of tenant, request key,
  connector and provider account, joined by U+001F), so no admission
  path can store a caller-chosen key; a unique index on
  (provider_account_id, provider_idempotency_key) is the backstop.
- RETRY_ELIGIBLE -> ESCALATED_UNRESOLVED (documented deviation from the
  section 05 diagram): retry-eligible work whose reclaim the database
  finds blocked (abandoned_claim_limit, or attempt_budget_exhausted,
  for example with no registered contract) goes to human review
  instead of being parked. The reason must equal
  safeact_reclaim_blocked(); anything else is SAT01.
- safeact_reopen_evidence() tells the reconciler that a decisive
  evidence row was recorded after the current escalation (for example
  a late worker reply), so it can reopen without any provider call. The
  0005 rules (one row per reopen, max_reopens) still apply in append.

Pinned by tests/phase_a/test_migration_sql_frozen.py. Values are
literals; nothing is read from core.
"""

from psycopg import sql

from storage.lifecycle_sql import v0003, v0005, v0006, v0007


REVISION = "0008_keys_escalation"

KEY_CONSTRAINT = "ck_safeact_operations_provider_key_derived"
KEY_INDEX = "uq_safeact_operations_provider_key"

NEW_TABLES = ()
NEW_INDEXES = (KEY_INDEX,)
HISTORY_TABLES = ()
LEGAL_TRANSITIONS = frozenset({("RETRY_ELIGIBLE", "ESCALATED_UNRESOLVED")})

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
    ("RETRY_ELIGIBLE", "ESCALATED_UNRESOLVED"),
})

NEW_FUNCTIONS = {
    "safeact_reclaim_blocked": "uuid",
    "safeact_reopen_evidence": "uuid",
}
REPLACED_FUNCTIONS = (
    "safeact_lifecycle_append",
    "safeact_recovery_transition",
)

SHARED_FUNCTIONS = {}
WORKER_FUNCTIONS = {}
RECOVERY_FUNCTIONS = {
    "safeact_reclaim_blocked": "uuid",
    "safeact_reopen_evidence": "uuid",
}
INTERNAL_FUNCTIONS = {}
OWNER_TABLE_PRIVILEGES = {}

# The derivation, recomputed by the database. Must equal
# core.provider_keys.derive_provider_key for native-key connectors.
DERIVED_KEY = (
    "'sa1_' || encode(sha256(convert_to("
    "tenant_id || chr(31) || request_key || chr(31) || "
    "connector_id || chr(31) || provider_account_id, 'UTF8')), 'hex')"
)

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


def _values(values):
    return sql.SQL(", ").join(sql.Literal(v) for v in sorted(values))


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
        lease_active=sql.Literal(v0003.LEASE_NOT_EXPIRED),
        unknown=sql.Literal(v0003.UNKNOWN_OPERATION),
        evidence_required=sql.Literal(v0003.EVIDENCE_REQUIRED),
        deadline_passed=sql.Literal(v0005.DEADLINE_PASSED),
        reopen_refused=sql.Literal(v0005.REOPEN_REFUSED),
        retry_unprotected=sql.Literal(v0007.RETRY_NOT_PROTECTED),
        budget_exhausted=sql.Literal(v0007.RETRY_BUDGET_EXHAUSTED),
        max_abandoned=sql.Literal(v0007.MAX_ABANDONED_CLAIMS),
        recovery=_pairs(RECOVERY_TRANSITIONS),
    )


def _reclaim_blocked(s):
    return _function(
        s, "safeact_reclaim_blocked", "p_operation uuid", "text", """
BEGIN
    -- Same limits safeact_claim applies to a reclaim (0007).
    IF (
        SELECT count(*) FROM {s}.safeact_operation_transitions a
        WHERE a.operation_id = p_operation
          AND a.from_state = 'CLAIMED'
          AND a.to_state IN ('RETRY_ELIGIBLE', 'UNKNOWN')
    ) >= {max_abandoned} THEN
        RETURN 'abandoned_claim_limit';
    END IF;

    IF (
        SELECT count(*) FROM {s}.safeact_attempts a
        WHERE a.operation_id = p_operation
    ) >= {s}.safeact_attempt_budget(p_operation) THEN
        RETURN 'attempt_budget_exhausted';
    END IF;

    RETURN NULL;
END;""", verb="CREATE", stable=True)


def _reopen_evidence(s):
    return _function(
        s, "safeact_reopen_evidence", "p_operation uuid", "boolean", """
DECLARE
    v_seq integer;
    v_state text;
    v_at timestamptz;
BEGIN
    SELECT t.seq, t.to_state, t.created_at INTO v_seq, v_state, v_at
    FROM {s}.safeact_operation_transitions t
    WHERE t.operation_id = p_operation
    ORDER BY t.seq DESC
    LIMIT 1;

    IF v_state IS DISTINCT FROM 'ESCALATED_UNRESOLVED' THEN
        RETURN false;
    END IF;

    -- The same evidence test the append function applies to a reopen.
    RETURN EXISTS (
        SELECT 1 FROM {s}.safeact_attempt_outcomes x
        WHERE x.operation_id = p_operation
          AND x.observed_seq = v_seq
          AND x.created_at > v_at
          AND x.evidence_reference IS NOT NULL
          AND ((x.outcome = 'confirmed_applied'
                AND x.evidence_kind = 'applied')
            OR (x.outcome = 'proven_not_applied'
                AND x.evidence_kind = 'proven_not_applied'))
    );
END;""", verb="CREATE", stable=True)


def _key_statements(s):
    return [
        sql.SQL(
            "ALTER TABLE {s}.safeact_operations ADD CONSTRAINT {name} "
            "CHECK (provider_idempotency_key IS NULL "
            "OR provider_idempotency_key = " + DERIVED_KEY + ")"
        ).format(s=s, name=sql.Identifier(KEY_CONSTRAINT)),
        sql.SQL(
            "CREATE UNIQUE INDEX {name} ON {s}.safeact_operations "
            "(provider_account_id, provider_idempotency_key) "
            "WHERE provider_idempotency_key IS NOT NULL"
        ).format(s=s, name=sql.Identifier(KEY_INDEX)),
    ]


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

    -- A retry is reachable only while the database itself finds it
    -- protected: proof of non-application for every earlier attempt,
    -- or a native key still inside its retention cutoff. Never from
    -- caller-supplied evidence. Within the contract's attempt budget.
    IF p_from = 'VERIFYING' AND p_to = 'RETRY_ELIGIBLE' THEN
        IF {s}.safeact_retry_basis(p_operation)
           IS DISTINCT FROM 'proof'
           AND {s}.safeact_retry_basis(p_operation)
           IS DISTINCT FROM 'same_key' THEN
            RAISE EXCEPTION 'Retry is not protected'
                USING ERRCODE = {retry_unprotected};
        END IF;

        IF (
            SELECT count(*) FROM {s}.safeact_attempts a
            WHERE a.operation_id = p_operation
        ) >= {s}.safeact_attempt_budget(p_operation) THEN
            RAISE EXCEPTION 'Attempt budget exhausted'
                USING ERRCODE = {budget_exhausted};
        END IF;
    END IF;

    -- Retry-eligible work that can no longer be reclaimed goes to a
    -- human instead of being parked: only when the database itself
    -- finds reclaim blocked, and with that exact reason.
    IF p_from = 'RETRY_ELIGIBLE' AND p_to = 'ESCALATED_UNRESOLVED'
       AND p_reason IS DISTINCT FROM
           {s}.safeact_reclaim_blocked(p_operation) THEN
        RAISE EXCEPTION 'Reclaim is not blocked by %', p_reason
            USING ERRCODE = {illegal};
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
    return _key_statements(s) + [
        _seed(s, LEGAL_TRANSITIONS),
        _reclaim_blocked(s),
        _reopen_evidence(s),
        _append(s),
        _recovery(s),
    ] + [
        sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(
            _signature(s, name, arguments)
        )
        for name, arguments in NEW_FUNCTIONS.items()
    ]


def downgrade_statements(schema):
    s = v0003._schema(schema)
    previous = {
        "safeact_lifecycle_append": v0007._append(s),
        "safeact_recovery_transition": v0006._recovery(s),
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
        for name, arguments in reversed(tuple(NEW_FUNCTIONS.items()))
    ] + [
        sql.SQL("ALTER TABLE {} DISABLE TRIGGER {}").format(table, guard),
        sql.SQL(
            "DELETE FROM {} WHERE (from_state, to_state) IN ({})"
        ).format(table, _pairs(LEGAL_TRANSITIONS)),
        sql.SQL("ALTER TABLE {} ENABLE TRIGGER {}").format(table, guard),
        sql.SQL("DROP INDEX IF EXISTS {s}.{name}").format(
            s=s, name=sql.Identifier(KEY_INDEX)
        ),
        sql.SQL(
            "ALTER TABLE {s}.safeact_operations DROP CONSTRAINT IF EXISTS {name}"
        ).format(s=s, name=sql.Identifier(KEY_CONSTRAINT)),
    ]
