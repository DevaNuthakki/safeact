"""FROZEN once committed: SQL for revision 0015_review_followups.

Review follow-ups (Milestone 3).

- Expired retry approval: when a contract's review policy requires an
  approval to retry and none arrives before the investigation deadline,
  the reconciler records VERIFYING -> FAILED_CONFIRMED with the reason
  'retry_approval_expired' ("not done; you can resubmit"), reported
  apart from a permanent provider rejection. The append function
  accepts that reason only for that move, only on such a contract,
  only after the deadline with no live approval, and only when every
  attempt has system-provenance proof of non-application (never a
  human attestation). The general FAILED_CONFIRMED rule still applies.
- Direct verification requests (blueprint 06 request_verification): an
  investigator may ask for one lookup without approval, lookup-only and
  inside the contract's bounds: after the post-escalation check
  interval since the last lookup, before the horizon, at most three per
  escalation. Checked when requested and again when the reconciler takes
  it. A reopen still needs decisive evidence. Going beyond the bounds
  still needs a reviewer-approved REVERIFY proposal.
- The review policy in force is part of every proposal's binding: the
  digest now covers the contract's policy row, so a policy row appearing
  after a proposal makes it stale (the table is insert-only, keyed to
  (connector_id, version)).
- safeact_outcome_reasons reports finished work by its final reason.

Pinned by tests/phase_a/test_migration_sql_frozen.py. Values are
literals; nothing is read from core.
"""

from psycopg import sql

from storage.lifecycle_sql import v0003, v0013


REVISION = "0015_review_followups"

APPROVAL_EXPIRED_REASON = "retry_approval_expired"
MAX_VERIFICATION_REQUESTS = 3

NEW_TABLES = (
    "safeact_verification_requests",
    "safeact_verification_request_uses",
)
HISTORY_TABLES = tuple(reversed(NEW_TABLES))
NEW_INDEXES = ("ix_safeact_verification_requests_operation",)
LEGAL_TRANSITIONS = frozenset()

NEW_FUNCTIONS = {
    "safeact_verification_bounds": "uuid",
    "safeact_api_request_verification": "text, text, uuid",
    "safeact_take_verification_request": "uuid",
    "safeact_outcome_reasons": "",
}
REPLACED_FUNCTIONS = (
    "safeact_lifecycle_append",
    "safeact_proposal_digest",
)

SHARED_FUNCTIONS = {}
WORKER_FUNCTIONS = {}
ADVANCER_FUNCTIONS = {}
INTERNAL_FUNCTIONS = {"safeact_verification_bounds": "uuid"}
API_FUNCTIONS = {"safeact_api_request_verification": "text, text, uuid"}
RECOVERY_FUNCTIONS = {
    "safeact_take_verification_request": "uuid",
    "safeact_outcome_reasons": "",
}
OWNER_TABLE_PRIVILEGES = {
    "safeact_verification_requests": ("SELECT", "INSERT"),
    "safeact_verification_request_uses": ("SELECT", "INSERT"),
}

_HEADER = (
    "LANGUAGE plpgsql {security} "
    "SET search_path = pg_catalog, pg_temp "
    "AS $safeact_lifecycle$ "
)


def _function(s, name, args, returns, body, *, definer=True,
              volatility="VOLATILE"):
    header = (
        "CREATE FUNCTION {s}.{name}(" + args + ") RETURNS "
        + returns + " " + volatility + " "
        + _HEADER.format(
            security="SECURITY DEFINER" if definer else "SECURITY INVOKER"
        )
    )
    return sql.SQL(header + body + " $safeact_lifecycle$").format(
        s=s,
        name=sql.Identifier(name),
        unknown=sql.Literal(v0003.UNKNOWN_OPERATION),
        illegal=sql.Literal(v0003.ILLEGAL_TRANSITION),
        not_eligible=sql.Literal(v0013.NOT_ELIGIBLE),
        expired=sql.Literal(APPROVAL_EXPIRED_REASON),
        max_requests=sql.Literal(MAX_VERIFICATION_REQUESTS),
        view=sql.Identifier(v0003.CURRENT_STATE_VIEW),
    )


def _tables(s):
    return [
        sql.SQL("""
CREATE TABLE {s}.safeact_verification_requests (
    request_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL
        REFERENCES {s}.safeact_operations (operation_id)
        ON DELETE RESTRICT,
    requested_by uuid NOT NULL
        REFERENCES {s}.safeact_principals (principal_id)
        ON DELETE RESTRICT,
    bound_seq integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
)""").format(s=s),
        sql.SQL("""
CREATE TABLE {s}.safeact_verification_request_uses (
    request_id uuid PRIMARY KEY
        REFERENCES {s}.safeact_verification_requests (request_id)
        ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now()
)""").format(s=s),
        sql.SQL(
            "CREATE INDEX ix_safeact_verification_requests_operation "
            "ON {s}.safeact_verification_requests (operation_id, bound_seq)"
        ).format(s=s),
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


def _append(s):
    # The frozen 0013 append, plus the guard for the expired-approval
    # reason. Everything else, including the general FAILED_CONFIRMED
    # evidence rule, is unchanged.
    base = v0013._append(s).as_string(None)
    marker = (
        "    INSERT INTO " + s.as_string(None)
        + ".safeact_operation_transitions ("
    )
    if base.count(marker) != 1:
        raise RuntimeError("0013 append no longer has one transition insert")
    head, tail = base.split(marker)
    guard = sql.SQL("""    -- 0015: "retry approval expired" is a confirmed failure that names
    -- why: a reviewed contract, past its investigation deadline, no live
    -- approval, and system proof of non-application for every attempt.
    IF p_reason = {expired} AND (
        p_from <> 'VERIFYING' OR p_to <> 'FAILED_CONFIRMED'
        OR NOT EXISTS (
            SELECT 1
            FROM {s}.safeact_operations o
            JOIN {s}.safeact_contract_review_policies r
              ON r.connector_id = o.connector_id
             AND r.version = o.contract_version
            WHERE o.operation_id = p_operation
              AND r.retry_requires_approval
        )
        OR coalesce({s}.safeact_investigation_deadline(p_operation),
                    'infinity'::timestamptz) > clock_timestamp()
        OR {s}.safeact_retry_approved(p_operation, p_seq - 1)
        OR EXISTS (
            SELECT 1 FROM {s}.safeact_attempts a
            WHERE a.operation_id = p_operation
              AND NOT EXISTS (
                  SELECT 1 FROM {s}.safeact_attempt_outcomes x
                  WHERE x.attempt_id = a.attempt_id
                    AND x.outcome = 'proven_not_applied'
                    AND x.evidence_kind = 'proven_not_applied'
                    AND x.evidence_reference IS NOT NULL
                    AND x.provenance = 'system'
              )
        )
    ) THEN
        RAISE EXCEPTION 'Retry approval has not expired for this operation'
            USING ERRCODE = {illegal};
    END IF;

""").format(
        s=s, expired=sql.Literal(APPROVAL_EXPIRED_REASON),
        illegal=sql.Literal(v0003.ILLEGAL_TRANSITION),
    ).as_string(None)
    return sql.SQL(head + guard + marker + tail)


def _digest(s):
    # The frozen 0013 binding, plus the review policy row in force.
    base = v0013._digest(s).as_string(None)
    old = "coalesce(p_reference, ''),"
    if base.count(old) != 1:
        raise RuntimeError("0013 digest changed shape")
    policy = (
        "coalesce((SELECT r.retry_requires_approval::text || '@' || "
        "to_char(r.created_at AT TIME ZONE 'UTC', "
        "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') "
        "FROM " + s.as_string(None) + ".safeact_contract_review_policies r "
        "WHERE r.connector_id = v.connector AND r.version = v.version), "
        "'no_policy'),"
    )
    text = base.replace(old, old + "\n            " + policy)
    return sql.SQL("CREATE OR REPLACE" + text[len("CREATE"):])


def _bounds(s):
    # NULL when a directly requested lookup is inside the contract's
    # bounds now; otherwise why not.
    return _function(s, "safeact_verification_bounds", "p_operation uuid",
                     "text", """
DECLARE
    v_status record;
    v_contract record;
    v_attempts integer;
BEGIN
    SELECT * INTO v_status
      FROM {s}.safeact_investigation_status(p_operation);
    SELECT k.lookup_semantics, k.post_escalation_check_interval_seconds
               AS interval_seconds,
           k.post_escalation_horizon_seconds AS horizon_seconds
      INTO v_contract
      FROM {s}.safeact_operations o
      JOIN {s}.safeact_connector_contracts k
        ON k.connector_id = o.connector_id AND k.version = o.contract_version
     WHERE o.operation_id = p_operation;
    SELECT count(*) INTO v_attempts
      FROM {s}.safeact_attempts a WHERE a.operation_id = p_operation;

    IF v_status.state IN ('UNKNOWN', 'VERIFYING') THEN
        RETURN 'verification_already_scheduled';
    ELSIF v_status.state IS DISTINCT FROM 'ESCALATED_UNRESOLVED' THEN
        RETURN 'wrong_state';
    ELSIF v_attempts = 0 THEN
        RETURN 'no_attempts';
    ELSIF v_contract.lookup_semantics IS NULL
          OR v_contract.lookup_semantics = 'unavailable' THEN
        RETURN 'lookup_unavailable';
    ELSIF v_status.observed_at >= v_status.escalated_at
          + make_interval(secs => v_contract.horizon_seconds) THEN
        RETURN 'beyond_horizon';
    ELSIF v_status.last_lookup_at IS NOT NULL
          AND v_status.observed_at < v_status.last_lookup_at
              + make_interval(secs => v_contract.interval_seconds) THEN
        RETURN 'too_soon';
    END IF;
    RETURN NULL;
END;
""", definer=False, volatility="STABLE")


def _request(s):
    return _function(
        s, "safeact_api_request_verification",
        "p_secret text, p_csrf text, p_operation uuid", "jsonb", """
DECLARE
    v record;
    v_lock record;
    v_reason text;
    v_id uuid := gen_random_uuid();
BEGIN
    SELECT * INTO v
      FROM {s}.safeact_api_session(p_secret, p_csrf, true, 'investigator');

    IF p_operation IS NULL OR NOT {s}.safeact_api_visible(
        p_operation, v.principal_id, v.tenant_id, v.roles
    ) THEN
        RAISE EXCEPTION 'Unknown operation' USING ERRCODE = {unknown};
    END IF;

    SELECT * INTO v_lock FROM {s}.safeact_lifecycle_lock(p_operation);
    v_reason := {s}.safeact_verification_bounds(p_operation);
    IF v_reason IS NULL AND (
        SELECT count(*) FROM {s}.safeact_verification_requests q
        WHERE q.operation_id = p_operation AND q.bound_seq = v_lock.o_seq
    ) >= {max_requests} THEN
        v_reason := 'request_limit_reached';
    END IF;
    IF v_reason IS NOT NULL THEN
        RAISE EXCEPTION 'Verification request outside bounds: %', v_reason
            USING ERRCODE = {not_eligible};
    END IF;

    INSERT INTO {s}.safeact_verification_requests (
        request_id, operation_id, requested_by, bound_seq
    ) VALUES (v_id, p_operation, v.principal_id, v_lock.o_seq);
    INSERT INTO {s}.safeact_audit_events (
        operation_id, event_type, actor_id, details
    ) VALUES (
        p_operation, 'VERIFICATION_REQUESTED', v.principal_id::text,
        jsonb_build_object('schema_version', 1, 'request_id', v_id,
                           'bound_seq', v_lock.o_seq)
    );

    RETURN jsonb_build_object(
        'request_id', v_id, 'operation_id', p_operation,
        'bound_seq', v_lock.o_seq, 'status', 'queued'
    );
END;
""")


def _take(s):
    return _function(s, "safeact_take_verification_request",
                     "p_operation uuid", "uuid", """
DECLARE
    v_lock record;
    v_request uuid;
BEGIN
    SELECT * INTO v_lock FROM {s}.safeact_lifecycle_lock(p_operation);
    -- The bounds are checked again when the lookup would run.
    IF {s}.safeact_verification_bounds(p_operation) IS NOT NULL THEN
        RETURN NULL;
    END IF;

    SELECT q.request_id INTO v_request
      FROM {s}.safeact_verification_requests q
     WHERE q.operation_id = p_operation
       AND q.bound_seq = v_lock.o_seq
       AND NOT EXISTS (
           SELECT 1 FROM {s}.safeact_verification_request_uses u
           WHERE u.request_id = q.request_id
       )
     ORDER BY q.created_at
     LIMIT 1;

    IF v_request IS NOT NULL THEN
        INSERT INTO {s}.safeact_verification_request_uses (request_id)
        VALUES (v_request);
    END IF;
    RETURN v_request;
END;
""")


def _outcome_reasons(s):
    return _function(
        s, "safeact_outcome_reasons", "",
        "TABLE (state text, reason text, operations bigint)", """
#variable_conflict use_column
BEGIN
    -- Finished work by the reason of its final transition, so a retry
    -- approval that expired is never read as a provider rejection.
    RETURN QUERY
    SELECT c.state::text, t.reason::text, count(*)
      FROM {s}.{view} c
      JOIN {s}.safeact_operation_transitions t
        ON t.operation_id = c.operation_id AND t.seq = c.seq
     WHERE c.state IN ('SUCCEEDED', 'FAILED_CONFIRMED',
                       'CANCELLED_BEFORE_CLAIM')
     GROUP BY 1, 2;
END;
""", volatility="STABLE")


def _signature(s, name, arguments):
    return sql.SQL("{}.{}({})").format(
        s, sql.Identifier(name), sql.SQL(arguments)
    )


def upgrade_statements(schema):
    s = v0003._schema(schema)
    return _tables(s) + [
        _append(s), _digest(s), _bounds(s), _request(s), _take(s),
        _outcome_reasons(s),
    ] + [
        sql.SQL("REVOKE ALL ON TABLE {}.{} FROM PUBLIC").format(
            s, sql.Identifier(name)
        )
        for name in NEW_TABLES
    ] + [
        sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(
            _signature(s, name, arguments)
        )
        for name, arguments in NEW_FUNCTIONS.items()
    ]


def downgrade_statements(schema):
    s = v0003._schema(schema)
    digest = v0013._digest(s).as_string(None)
    return [
        v0013._append(s),
        sql.SQL("CREATE OR REPLACE" + digest[len("CREATE"):]),
    ] + [
        sql.SQL("DROP FUNCTION IF EXISTS {}").format(
            _signature(s, name, arguments)
        )
        for name, arguments in reversed(tuple(NEW_FUNCTIONS.items()))
    ] + [
        sql.SQL("DROP TABLE IF EXISTS {}.{}").format(
            s, sql.Identifier(table)
        )
        for table in reversed(NEW_TABLES)
    ]
