"""FROZEN once committed: SQL for revision 0007_same_key_retry.

Same-key retry from RETRY_ELIGIBLE, guarded by the database.

- safeact_retry_basis(op) is the single SQL definition of a protected
  retry: 'first' (no attempt yet), 'proof' (every earlier attempt has
  proven_not_applied evidence and none has applied evidence), or
  'same_key' (the contract's native key binds parameters, protects
  concurrent requests, a provider key exists, and the clock is before
  first fence + retention - safety margin). NULL means not protected.
- VERIFYING -> RETRY_ELIGIBLE requires 'proof' or 'same_key' (SAT12)
  within the contract's attempt budget (SAT13), on every path.
- safeact_claim reclaims RETRY_ELIGIBLE within the attempt budget and
  at most 3 abandoned claims (SAT13), so an intent that can never be
  dispatched cannot cycle claim -> abandon -> reclaim forever.
- safeact_begin_dispatch rechecks the basis for every attempt after
  the first (SAT12) and the attempt budget (SAT13): retention may
  lapse between the decision and the redispatch (F09).
- safeact_record_dispatch_outcome accepts DISPATCHING -> UNKNOWN only
  with a reason from a fixed vocabulary. The retry-pending reason
  requires proven_not_applied evidence on that attempt; ambiguous
  reasons must not carry decisive evidence (SAT09).
- The claimable and relay scans include reclaimable RETRY_ELIGIBLE
  work; safeact_accounting splits UNKNOWN work by reason.

Pinned by tests/phase_a/test_migration_sql_frozen.py. Values are
literals; nothing is read from core.
"""

from psycopg import sql

from storage.lifecycle_sql import v0003, v0004, v0005, v0006


REVISION = "0007_same_key_retry"

RETRY_NOT_PROTECTED = "SAT12"
RETRY_BUDGET_EXHAUSTED = "SAT13"

MAX_ABANDONED_CLAIMS = 3

RETRY_PENDING_REASON = "rejected_before_execution_retry_pending"
AMBIGUOUS_REASONS = (
    "ambiguous_error_after_send",
    "ambiguous_key_conflict",
    "ambiguous_no_response",
    "provider_in_flight_conflict",
    "request_deadline_elapsed_before_send",
)

NEW_TABLES = ()
HISTORY_TABLES = ()
LEGAL_TRANSITIONS = frozenset()

NEW_FUNCTIONS = {
    "safeact_retry_basis": "uuid",
    "safeact_attempt_budget": "uuid",
    "safeact_accounting": "",
}
REPLACED_FUNCTIONS = (
    "safeact_lifecycle_append",
    "safeact_claim",
    "safeact_begin_dispatch",
    "safeact_record_dispatch_outcome",
    "safeact_claimable_operations",
    "safeact_relay_pending",
)

SHARED_FUNCTIONS = {}
WORKER_FUNCTIONS = {}
RECOVERY_FUNCTIONS = {"safeact_accounting": ""}
INTERNAL_FUNCTIONS = {
    "safeact_retry_basis": "uuid",
    "safeact_attempt_budget": "uuid",
}
OWNER_TABLE_PRIVILEGES = {}

_HEADER = (
    "LANGUAGE plpgsql {security} "
    "SET search_path = pg_catalog, pg_temp "
    "AS $safeact_lifecycle$ "
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
        not_owner=sql.Literal(v0003.NOT_CLAIM_OWNER),
        lease_expired=sql.Literal(v0003.CLAIM_LEASE_EXPIRED),
        incomplete=sql.Literal(v0003.ADMISSION_INCOMPLETE),
        evidence_required=sql.Literal(v0003.EVIDENCE_REQUIRED),
        contract_unknown=sql.Literal(v0004.CONTRACT_UNKNOWN),
        deadline_passed=sql.Literal(v0005.DEADLINE_PASSED),
        reopen_refused=sql.Literal(v0005.REOPEN_REFUSED),
        retry_unprotected=sql.Literal(RETRY_NOT_PROTECTED),
        budget_exhausted=sql.Literal(RETRY_BUDGET_EXHAUSTED),
        max_abandoned=sql.Literal(MAX_ABANDONED_CLAIMS),
        max_lease=sql.Literal(v0003.MAX_LEASE_SECONDS),
        max_page=sql.Literal(v0003.MAX_PAGE),
        pending_reason=sql.Literal(RETRY_PENDING_REASON),
        ambiguous=_values(AMBIGUOUS_REASONS),
        view=sql.Identifier(v0003.CURRENT_STATE_VIEW),
        claimable=sql.SQL(_CLAIMABLE).format(
            s=s, max_abandoned=sql.Literal(MAX_ABANDONED_CLAIMS),
        ),
    )


# Never-claimed work, plus RETRY_ELIGIBLE work still within the
# abandoned-claim cap. Ordered by admission time.
_CLAIMABLE = """
    SELECT c.operation_id FROM (
        SELECT o.operation_id, o.created_at
        FROM {s}.safeact_operations o
        WHERE NOT EXISTS (
            SELECT 1 FROM {s}.safeact_operation_transitions t
            WHERE t.operation_id = o.operation_id
        )
        UNION ALL
        SELECT o.operation_id, o.created_at
        FROM {s}.safeact_operation_transitions t
        JOIN {s}.safeact_operations o
          ON o.operation_id = t.operation_id
        WHERE t.to_state = 'RETRY_ELIGIBLE'
          AND NOT EXISTS (
              SELECT 1 FROM {s}.safeact_operation_transitions n
              WHERE n.operation_id = t.operation_id
                AND n.seq > t.seq
          )
          AND (
              SELECT count(*) FROM {s}.safeact_operation_transitions a
              WHERE a.operation_id = t.operation_id
                AND a.from_state = 'CLAIMED'
                AND a.to_state IN ('RETRY_ELIGIBLE', 'UNKNOWN')
          ) < {max_abandoned}
    ) c
    ORDER BY c.created_at, c.operation_id
    LIMIT p_limit"""


def _retry_basis(s):
    return _function(
        s, "safeact_retry_basis", "p_operation uuid", "text", """
DECLARE
    c record;
    v_key text;
    v_first timestamptz;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM {s}.safeact_attempts a
        WHERE a.operation_id = p_operation
    ) THEN
        RETURN 'first';
    END IF;

    -- A known effect is never retried.
    IF EXISTS (
        SELECT 1 FROM {s}.safeact_attempt_outcomes x
        WHERE x.operation_id = p_operation
          AND (x.outcome = 'confirmed_applied'
               OR x.evidence_kind = 'applied')
    ) THEN
        RETURN NULL;
    END IF;

    -- Every earlier attempt proven not applied: none can apply later.
    IF NOT EXISTS (
        SELECT 1 FROM {s}.safeact_attempts a
        WHERE a.operation_id = p_operation
          AND NOT EXISTS (
              SELECT 1 FROM {s}.safeact_attempt_outcomes x
              WHERE x.attempt_id = a.attempt_id
                AND x.outcome = 'proven_not_applied'
                AND x.evidence_kind = 'proven_not_applied'
                AND x.evidence_reference IS NOT NULL
          )
    ) THEN
        RETURN 'proof';
    END IF;

    SELECT k.native_idempotency, k.key_binds_parameters,
           k.key_protects_concurrent, k.key_retention_seconds,
           k.key_safety_margin_seconds
    INTO c
    FROM {s}.safeact_operations o
    JOIN {s}.safeact_connector_contracts k
      ON k.connector_id = o.connector_id
     AND k.version = o.contract_version
    WHERE o.operation_id = p_operation;

    SELECT o.provider_idempotency_key INTO v_key
    FROM {s}.safeact_operations o
    WHERE o.operation_id = p_operation;

    -- The key was first used when the first fence committed.
    SELECT min(t.created_at) INTO v_first
    FROM {s}.safeact_operation_transitions t
    WHERE t.operation_id = p_operation
      AND t.to_state = 'DISPATCHING';

    IF c.native_idempotency IS TRUE
       AND c.key_binds_parameters IS TRUE
       AND c.key_protects_concurrent IS TRUE
       AND v_key IS NOT NULL
       AND clock_timestamp() < v_first + make_interval(
           secs => c.key_retention_seconds - c.key_safety_margin_seconds
       ) THEN
        RETURN 'same_key';
    END IF;

    RETURN NULL;
END;""", verb="CREATE", definer=False, stable=True)


def _attempt_budget(s):
    return _function(
        s, "safeact_attempt_budget", "p_operation uuid", "integer", """
BEGIN
    -- No registered contract: no budget at all.
    RETURN COALESCE((
        SELECT k.max_attempts
        FROM {s}.safeact_operations o
        JOIN {s}.safeact_connector_contracts k
          ON k.connector_id = o.connector_id
         AND k.version = o.contract_version
        WHERE o.operation_id = p_operation
    ), 0);
END;""", verb="CREATE", definer=False, stable=True)


def _accounting(s):
    return _function(
        s, "safeact_accounting", "",
        "TABLE(state text, unknown_reason text, operations bigint)", """
BEGIN
    -- Every admitted operation, by current state; UNKNOWN and
    -- VERIFYING work split by why the dispatch outcome was unknown.
    RETURN QUERY
    SELECT c.state::text,
           CASE WHEN c.state IN ('UNKNOWN', 'VERIFYING') THEN (
               SELECT t.reason::text
               FROM {s}.safeact_operation_transitions t
               WHERE t.operation_id = c.operation_id
                 AND t.from_state = 'DISPATCHING'
                 AND t.to_state = 'UNKNOWN'
               ORDER BY t.seq DESC
               LIMIT 1
           ) END,
           count(*)
    FROM {s}.{view} c
    GROUP BY 1, 2;
END;""", verb="CREATE", stable=True)


def _claim(s):
    return _function(
        s, "safeact_claim", "p_operation uuid, p_lease_seconds integer",
        "TABLE(claim_id uuid, seq integer, tenant_id text, "
        "action_type text, resource_id text, provider_account_id text, "
        "connector_id text, contract_version text, payload_sha256 text, "
        "payload_canonical text, provider_idempotency_key text)", """
DECLARE
    v record;
    v_claim uuid := gen_random_uuid();
    v_retry boolean;
BEGIN
    IF p_lease_seconds IS NULL
       OR p_lease_seconds NOT BETWEEN 1 AND {max_lease} THEN
        RAISE EXCEPTION 'Invalid lease' USING ERRCODE = '22023';
    END IF;

    SELECT * INTO v FROM {s}.safeact_lifecycle_lock(p_operation);
    v_retry := v.o_state = 'RETRY_ELIGIBLE';

    IF v_retry THEN
        IF (
            SELECT count(*) FROM {s}.safeact_operation_transitions a
            WHERE a.operation_id = p_operation
              AND a.from_state = 'CLAIMED'
              AND a.to_state IN ('RETRY_ELIGIBLE', 'UNKNOWN')
        ) >= {max_abandoned} THEN
            RAISE EXCEPTION 'Abandoned-claim budget exhausted'
                USING ERRCODE = {budget_exhausted};
        END IF;

        IF (
            SELECT count(*) FROM {s}.safeact_attempts a
            WHERE a.operation_id = p_operation
        ) >= {s}.safeact_attempt_budget(p_operation) THEN
            RAISE EXCEPTION 'Attempt budget exhausted'
                USING ERRCODE = {budget_exhausted};
        END IF;
    ELSIF v.o_seq <> 0 THEN
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
        p_operation, v.o_seq + 1, v.o_state, 'CLAIMED', v_claim, NULL,
        clock_timestamp() + make_interval(secs => p_lease_seconds),
        CASE WHEN v_retry THEN 'reclaimed' ELSE 'claimed' END,
        '{{}}'::jsonb,
        CASE WHEN v_retry THEN 'LIFECYCLE_RECLAIMED'
             ELSE 'LIFECYCLE_CLAIMED' END
    );

    -- Dispatch inputs come from the immutable durable record read
    -- under this lock, never from the delivered task.
    RETURN QUERY
    SELECT v_claim, v.o_seq + 1,
           o.tenant_id::text, o.action_type::text,
           o.resource_id::text, o.provider_account_id::text,
           o.connector_id::text, o.contract_version::text,
           o.payload_sha256::text, o.payload_canonical,
           o.provider_idempotency_key::text
    FROM {s}.safeact_operations o
    WHERE o.operation_id = p_operation;
END;""", verb="CREATE OR REPLACE")


def _begin_dispatch(s):
    return _function(
        s, "safeact_begin_dispatch",
        "p_operation uuid, p_expected_seq integer, p_claim uuid, "
        "p_lease_seconds integer",
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

    -- No dispatch without a registered, versioned connector contract.
    PERFORM 1
    FROM {s}.safeact_operations o
    JOIN {s}.safeact_connector_contracts c
      ON c.connector_id = o.connector_id
     AND c.version = o.contract_version
    WHERE o.operation_id = p_operation;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Unknown connector contract'
            USING ERRCODE = {contract_unknown};
    END IF;

    SELECT count(*) + 1 INTO v_ordinal
    FROM {s}.safeact_attempts a
    WHERE a.operation_id = p_operation;

    IF v_ordinal > {s}.safeact_attempt_budget(p_operation) THEN
        RAISE EXCEPTION 'Attempt budget exhausted'
            USING ERRCODE = {budget_exhausted};
    END IF;

    -- Recheck at the fence: key retention may have lapsed since the
    -- retry decision. Only a protected repeat may be sent.
    IF v_ordinal > 1 AND {s}.safeact_retry_basis(p_operation)
       IS DISTINCT FROM 'proof'
       AND {s}.safeact_retry_basis(p_operation)
       IS DISTINCT FROM 'same_key' THEN
        RAISE EXCEPTION 'Retry is not protected'
            USING ERRCODE = {retry_unprotected};
    END IF;

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
END;""", verb="CREATE OR REPLACE")


def _dispatch_outcome(s):
    return _function(
        s, "safeact_record_dispatch_outcome",
        "p_operation uuid, p_expected_seq integer, p_claim uuid, "
        "p_to_state text, p_reason text, p_evidence jsonb",
        "integer", """
DECLARE
    v record;
    v_decisive boolean;
BEGIN
    SELECT * INTO v FROM {s}.safeact_lifecycle_lock(p_operation);

    IF v.o_seq IS DISTINCT FROM p_expected_seq THEN
        RAISE EXCEPTION 'Stale owner version'
            USING ERRCODE = {stale};
    END IF;

    IF v.o_state <> 'DISPATCHING'
       OR p_to_state IS NULL
       OR p_to_state NOT IN ('FAILED_CONFIRMED', 'SUCCEEDED', 'UNKNOWN') THEN
        RAISE EXCEPTION 'Illegal transition % -> %',
            v.o_state, p_to_state USING ERRCODE = {illegal};
    END IF;

    IF v.o_claim IS DISTINCT FROM p_claim
       OR v.o_actor IS DISTINCT FROM session_user::text THEN
        RAISE EXCEPTION 'Not the claim owner'
            USING ERRCODE = {not_owner};
    END IF;

    -- Why the outcome is unknown is part of the record: a definitive
    -- rejection awaiting retry is reported apart from ambiguity.
    IF p_to_state = 'UNKNOWN' THEN
        IF p_reason IS NULL
           OR (p_reason <> {pending_reason}
               AND p_reason NOT IN ({ambiguous})) THEN
            RAISE EXCEPTION 'Unknown-outcome reason % is not allowed',
                p_reason USING ERRCODE = '22023';
        END IF;

        v_decisive := EXISTS (
            SELECT 1 FROM {s}.safeact_attempt_outcomes x
            WHERE x.attempt_id = v.o_attempt
              AND x.evidence_reference IS NOT NULL
              AND ((x.outcome = 'proven_not_applied'
                    AND x.evidence_kind = 'proven_not_applied')
                OR (x.outcome = 'confirmed_applied'
                    AND x.evidence_kind = 'applied'))
        );

        IF p_reason = {pending_reason} AND NOT EXISTS (
            SELECT 1 FROM {s}.safeact_attempt_outcomes x
            WHERE x.attempt_id = v.o_attempt
              AND x.outcome = 'proven_not_applied'
              AND x.evidence_kind = 'proven_not_applied'
              AND x.evidence_reference IS NOT NULL
        ) THEN
            RAISE EXCEPTION 'Retry-pending rejection needs proof'
                USING ERRCODE = {evidence_required};
        END IF;

        IF p_reason <> {pending_reason} AND v_decisive THEN
            RAISE EXCEPTION 'Ambiguous reason contradicts evidence'
                USING ERRCODE = {evidence_required};
        END IF;
    END IF;

    PERFORM {s}.safeact_lifecycle_append(
        p_operation, v.o_seq + 1, 'DISPATCHING', p_to_state,
        v.o_claim, v.o_attempt, NULL, p_reason, p_evidence,
        'LIFECYCLE_DISPATCH_OUTCOME'
    );

    RETURN v.o_seq + 1;
END;""", verb="CREATE OR REPLACE")


def _claimable_function(s):
    return _function(
        s, "safeact_claimable_operations", "p_limit integer",
        "TABLE(operation_id uuid)", """
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND {max_page} THEN
        RAISE EXCEPTION 'Invalid page size' USING ERRCODE = '22023';
    END IF;

    RETURN QUERY {claimable};
END;""", verb="CREATE OR REPLACE", stable=True)


def _relay(s):
    return _function(
        s, "safeact_relay_pending", "p_limit integer",
        "TABLE(outbox_id uuid, operation_id uuid)", """
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND {max_page} THEN
        RAISE EXCEPTION 'Invalid page size' USING ERRCODE = '22023';
    END IF;

    -- Claimable work, including reclaimable retries. The relay may
    -- publish one more than once; the claim makes that harmless.
    RETURN QUERY
    SELECT e.outbox_id, e.operation_id
    FROM ({claimable}) c
    JOIN {s}.safeact_outbox_events e
      ON e.operation_id = c.operation_id
     AND e.event_type = 'INTENT_RECORDED_NO_DISPATCH'
     AND e.state = 'PENDING';
END;""", verb="CREATE OR REPLACE", stable=True)


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


def _signature(s, name, arguments):
    return sql.SQL("{}.{}({})").format(
        s, sql.Identifier(name), sql.SQL(arguments)
    )


def _replace(statement):
    text = statement.as_string(None)
    if text.startswith("CREATE OR REPLACE FUNCTION "):
        return statement
    if not text.startswith("CREATE FUNCTION "):
        raise ValueError("not a function definition")
    return sql.SQL("CREATE OR REPLACE" + text[len("CREATE"):])


def upgrade_statements(schema):
    s = v0003._schema(schema)
    return [
        _retry_basis(s),
        _attempt_budget(s),
        _accounting(s),
        _append(s),
        _claim(s),
        _begin_dispatch(s),
        _dispatch_outcome(s),
        _claimable_function(s),
        _relay(s),
    ] + [
        sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(
            _signature(s, name, arguments)
        )
        for name, arguments in NEW_FUNCTIONS.items()
    ]


def downgrade_statements(schema):
    s = v0003._schema(schema)

    def named(statements, name):
        for statement in statements:
            if f'."{name}"(' in statement.as_string(None)[:200]:
                return statement
        raise RuntimeError(f"earlier body of {name} not found")

    earlier = v0003._functions(s)
    previous = {
        "safeact_lifecycle_append": v0006._append(s),
        "safeact_claim": named(earlier, "safeact_claim"),
        "safeact_begin_dispatch": v0004._begin_dispatch(s),
        "safeact_record_dispatch_outcome":
            named(earlier, "safeact_record_dispatch_outcome"),
        "safeact_claimable_operations":
            named(earlier, "safeact_claimable_operations"),
        "safeact_relay_pending": named(earlier, "safeact_relay_pending"),
    }
    return [
        _replace(previous[name]) for name in REPLACED_FUNCTIONS
    ] + [
        sql.SQL("DROP FUNCTION IF EXISTS {}").format(
            _signature(s, name, arguments)
        )
        for name, arguments in reversed(tuple(NEW_FUNCTIONS.items()))
    ]
