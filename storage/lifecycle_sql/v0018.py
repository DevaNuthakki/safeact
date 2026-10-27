"""FROZEN once committed: SQL for revision 0018_provider_limits.

F16: 429s, long outages and retry storms (Milestone 5, M5.4).

Opt-in per contract: everything below applies only to operations whose
contract has a row in safeact_contract_limits. Contracts without one
behave exactly as before.

- Retry backoff: every move to RETRY_ELIGIBLE records a not_before in
  safeact_retry_schedule, in the same transaction. The delay is
  exponential in the number of attempts (base * 2^(n-1), capped), with
  full jitter derived deterministically from the operation id and the
  attempt count, and never shorter than a Retry-After the provider sent
  (recorded as an attempt fact). A reclaim before not_before is refused
  (SAT30) and the claimable scan leaves it out. The attempt budget
  (SAT13) still bounds the total.
- Provider breaker and dispatch rate, per connector and provider
  account, in PostgreSQL (so losing Redis resets nothing):
  * consecutive dispatch outcomes that say the provider is failing
    (no response, server error, rate limited, retryable rejection)
    open the breaker for a cooldown once they reach the threshold; a
    decisive outcome closes it. After the cooldown one claim is admitted
    as the half-open probe until its outcome is known;
  * a token bucket bounds claims per second.
  A claim refused by either raises SAT31 and appends nothing, so no
  claim budget is spent and nothing leaves RECORDED or RETRY_ELIGIBLE.
  Every breaker change is recorded in safeact_connector_health_events.
- Lookup pacing: the move UNKNOWN -> VERIFYING (one reconciler lookup)
  is refused (SAT30) before safeact_next_lookup_at: due at once after a
  dispatch, then after the lookup interval doubling per lookup since
  that dispatch, capped, and never later than the investigation
  deadline (so the last look happens on time and escalation follows).
- Dispatch-outcome vocabulary gains 'ambiguous_rate_limited' (a 429 a
  contract does not document as pre-execution).
- safeact_pacing(op) exposes next lookup, retry not_before and the
  breaker for the reconciler and the console's next check.

Pinned by tests/phase_a/test_migration_sql_frozen.py. Values are
literals; nothing is read from core.
"""

from psycopg import sql

from storage.lifecycle_sql import v0003, v0007, v0017


REVISION = "0018_provider_limits"

NOT_YET_DUE = "SAT30"
PROVIDER_LIMITED = "SAT31"
RATE_LIMITED_REASON = "ambiguous_rate_limited"

# Dispatch outcomes that say the provider (not our key) is failing.
FAILING_REASONS = (
    "ambiguous_error_after_send",
    "ambiguous_no_response",
    "ambiguous_rate_limited",
    "rejected_before_execution_retry_pending",
)

NEW_TABLES = (
    "safeact_contract_limits",
    "safeact_retry_schedule",
    "safeact_connector_health",
    "safeact_connector_health_events",
)
HISTORY_TABLES = (
    "safeact_connector_health_events",
    "safeact_retry_schedule",
)
NEW_INDEXES = ()
LEGAL_TRANSITIONS = frozenset()

NEW_FUNCTIONS = {
    "safeact_backoff_seconds": "uuid, integer",
    "safeact_next_lookup_at": "uuid",
    "safeact_dispatch_gate": "uuid, boolean",
    "safeact_pacing": "uuid",
}
REPLACED_FUNCTIONS = (
    "safeact_lifecycle_append",
    "safeact_claim",
    "safeact_record_dispatch_outcome",
    "safeact_claimable_operations",
    "safeact_relay_pending",
)

SHARED_FUNCTIONS = {}
WORKER_FUNCTIONS = {}
ADVANCER_FUNCTIONS = {}
RECOVERY_FUNCTIONS = {"safeact_pacing": "uuid"}
INTERNAL_FUNCTIONS = {
    "safeact_backoff_seconds": "uuid, integer",
    "safeact_next_lookup_at": "uuid",
    "safeact_dispatch_gate": "uuid, boolean",
}
OWNER_TABLE_PRIVILEGES = {
    "safeact_contract_limits": ("SELECT",),
    "safeact_retry_schedule": ("SELECT", "INSERT"),
    "safeact_connector_health": ("SELECT", "INSERT", "UPDATE"),
    "safeact_connector_health_events": ("SELECT", "INSERT"),
}

_HEADER = (
    "LANGUAGE plpgsql {security} "
    "SET search_path = pg_catalog, pg_temp "
    "AS $safeact_lifecycle$ "
)


def _values(values):
    return sql.SQL(", ").join(sql.Literal(v) for v in sorted(values))


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
        not_due=sql.Literal(NOT_YET_DUE),
        limited=sql.Literal(PROVIDER_LIMITED),
        unknown=sql.Literal(v0003.UNKNOWN_OPERATION),
        failing=_values(FAILING_REASONS),
    )


def _tables(s):
    return [
        sql.SQL("""
CREATE TABLE {s}.safeact_contract_limits (
    connector_id varchar(128) NOT NULL,
    version varchar(64) NOT NULL,
    dispatch_rate_per_second numeric(10,3),
    dispatch_burst integer,
    backoff_base_seconds numeric(10,3) NOT NULL,
    backoff_cap_seconds numeric(10,3) NOT NULL,
    lookup_interval_seconds numeric(10,3) NOT NULL,
    lookup_interval_cap_seconds numeric(10,3) NOT NULL,
    breaker_threshold integer,
    breaker_cooldown_seconds numeric(10,3),
    PRIMARY KEY (connector_id, version),
    FOREIGN KEY (connector_id, version)
        REFERENCES {s}.safeact_connector_contracts (connector_id, version)
        ON DELETE RESTRICT,
    CONSTRAINT ck_safeact_contract_limits_rate
        CHECK ((dispatch_rate_per_second IS NULL) = (dispatch_burst IS NULL)
               AND (dispatch_rate_per_second IS NULL
                    OR (dispatch_rate_per_second > 0 AND dispatch_burst >= 1))),
    CONSTRAINT ck_safeact_contract_limits_backoff
        CHECK (backoff_base_seconds > 0
               AND backoff_cap_seconds >= backoff_base_seconds),
    CONSTRAINT ck_safeact_contract_limits_lookup
        CHECK (lookup_interval_seconds > 0
               AND lookup_interval_cap_seconds >= lookup_interval_seconds),
    CONSTRAINT ck_safeact_contract_limits_breaker
        CHECK ((breaker_threshold IS NULL)
               = (breaker_cooldown_seconds IS NULL)
               AND (breaker_threshold IS NULL
                    OR (breaker_threshold >= 1
                        AND breaker_cooldown_seconds > 0)))
)""").format(s=s),
        sql.SQL("""
CREATE TABLE {s}.safeact_retry_schedule (
    operation_id uuid NOT NULL,
    seq integer NOT NULL,
    attempts integer NOT NULL,
    backoff_seconds numeric(12,3) NOT NULL,
    retry_after_seconds numeric(12,3),
    not_before timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (operation_id, seq),
    FOREIGN KEY (operation_id, seq)
        REFERENCES {s}.safeact_operation_transitions (operation_id, seq)
        ON DELETE RESTRICT,
    CONSTRAINT ck_safeact_retry_schedule_values
        CHECK (attempts >= 1 AND backoff_seconds >= 0
               AND (retry_after_seconds IS NULL OR retry_after_seconds >= 0))
)""").format(s=s),
        sql.SQL("""
CREATE TABLE {s}.safeact_connector_health (
    connector_id varchar(128) NOT NULL,
    provider_account_id varchar(255) NOT NULL,
    tokens numeric(12,3) NOT NULL,
    refilled_at timestamptz NOT NULL,
    consecutive_failures integer NOT NULL DEFAULT 0,
    open_until timestamptz,
    probe_until timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (connector_id, provider_account_id),
    CONSTRAINT ck_safeact_connector_health_values
        CHECK (tokens >= 0 AND consecutive_failures >= 0)
)""").format(s=s),
        sql.SQL("""
CREATE TABLE {s}.safeact_connector_health_events (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    connector_id varchar(128) NOT NULL,
    provider_account_id varchar(255) NOT NULL,
    event varchar(32) NOT NULL,
    consecutive_failures integer NOT NULL,
    operation_id uuid REFERENCES {s}.safeact_operations (operation_id)
        ON DELETE RESTRICT,
    until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_safeact_connector_health_events_event
        CHECK (event IN ('breaker_opened', 'breaker_closed', 'probe_admitted'))
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
        for table in (
            "safeact_contract_limits", "safeact_retry_schedule",
            "safeact_connector_health_events",
        )
    ]


def _backoff(s):
    return _function(
        s, "safeact_backoff_seconds", "p_operation uuid, p_attempts integer",
        "numeric", """
DECLARE
    l record;
    v_ceiling numeric;
    v_fraction numeric;
BEGIN
    SELECT k.backoff_base_seconds, k.backoff_cap_seconds INTO l
    FROM {s}.safeact_operations o
    JOIN {s}.safeact_contract_limits k
      ON k.connector_id = o.connector_id AND k.version = o.contract_version
    WHERE o.operation_id = p_operation;

    IF NOT FOUND OR p_attempts IS NULL OR p_attempts < 1 THEN
        RETURN 0;
    END IF;

    v_ceiling := least(
        l.backoff_cap_seconds,
        l.backoff_base_seconds * power(2::numeric, least(p_attempts - 1, 30))
    );
    -- Full jitter, deterministic: the same operation and attempt always
    -- wait the same time, so a seeded run replays it.
    v_fraction := ('x' || substr(md5(p_operation::text || ':'
                                     || p_attempts::text), 1, 8))::bit(32)
                  ::bigint / 4294967296.0;
    RETURN round(v_ceiling * v_fraction, 3);
END;""", definer=False, volatility="STABLE")


def _next_lookup(s):
    return _function(
        s, "safeact_next_lookup_at", "p_operation uuid", "timestamptz", """
DECLARE
    l record;
    v_dispatch timestamptz;
    v_count integer;
    v_last timestamptz;
    v_next timestamptz;
    v_deadline timestamptz;
BEGIN
    SELECT k.lookup_interval_seconds, k.lookup_interval_cap_seconds INTO l
    FROM {s}.safeact_operations o
    JOIN {s}.safeact_contract_limits k
      ON k.connector_id = o.connector_id AND k.version = o.contract_version
    WHERE o.operation_id = p_operation;

    IF NOT FOUND THEN
        RETURN NULL;          -- no pacing declared for this contract
    END IF;

    SELECT max(t.created_at) INTO v_dispatch
    FROM {s}.safeact_operation_transitions t
    WHERE t.operation_id = p_operation AND t.to_state = 'DISPATCHING';

    IF v_dispatch IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT count(*), max(x.created_at) INTO v_count, v_last
    FROM {s}.safeact_attempt_outcomes x
    WHERE x.operation_id = p_operation
      AND x.created_at >= v_dispatch
      AND x.details ->> 'source' = 'reconciler_lookup';

    IF v_count = 0 THEN
        RETURN v_dispatch;    -- the first lookup is due at once
    END IF;

    v_next := v_last + make_interval(secs => least(
        l.lookup_interval_cap_seconds,
        l.lookup_interval_seconds
            * power(2::numeric, least(v_count - 1, 30))
    )::double precision);
    v_deadline := {s}.safeact_investigation_deadline(p_operation);
    IF v_deadline IS NOT NULL AND v_next > v_deadline THEN
        v_next := greatest(v_deadline, v_last);
    END IF;
    RETURN v_next;
END;""", definer=False, volatility="STABLE")


def _gate(s):
    return _function(
        s, "safeact_dispatch_gate", "p_operation uuid, p_retry boolean",
        "void", """
DECLARE
    o record;
    l record;
    h record;
    v_now timestamptz := clock_timestamp();
    v_not_before timestamptz;
    v_tokens numeric;
BEGIN
    SELECT x.connector_id, x.contract_version, x.provider_account_id INTO o
    FROM {s}.safeact_operations x WHERE x.operation_id = p_operation;

    SELECT k.* INTO l FROM {s}.safeact_contract_limits k
    WHERE k.connector_id = o.connector_id AND k.version = o.contract_version;
    IF NOT FOUND THEN
        RETURN;               -- no limits declared for this contract
    END IF;

    IF p_retry THEN
        SELECT r.not_before INTO v_not_before
        FROM {s}.safeact_retry_schedule r
        WHERE r.operation_id = p_operation
        ORDER BY r.seq DESC LIMIT 1;
        IF v_not_before > v_now THEN
            RAISE EXCEPTION 'Retry not due before %', v_not_before
                USING ERRCODE = {not_due};
        END IF;
    END IF;

    IF l.dispatch_rate_per_second IS NULL AND l.breaker_threshold IS NULL THEN
        RETURN;
    END IF;

    INSERT INTO {s}.safeact_connector_health (
        connector_id, provider_account_id, tokens, refilled_at
    ) VALUES (
        o.connector_id, o.provider_account_id,
        coalesce(l.dispatch_burst, 0), v_now
    ) ON CONFLICT (connector_id, provider_account_id) DO NOTHING;

    SELECT * INTO h FROM {s}.safeact_connector_health x
    WHERE x.connector_id = o.connector_id
      AND x.provider_account_id = o.provider_account_id
    FOR UPDATE;

    IF l.breaker_threshold IS NOT NULL THEN
        IF h.open_until > v_now THEN
            RAISE EXCEPTION 'Provider breaker open until %', h.open_until
                USING ERRCODE = {limited};
        END IF;
        IF h.consecutive_failures >= l.breaker_threshold THEN
            -- Half-open: one probe at a time until its outcome is known.
            IF h.probe_until > v_now THEN
                RAISE EXCEPTION 'Provider breaker half-open, probe in flight'
                    USING ERRCODE = {limited};
            END IF;
            UPDATE {s}.safeact_connector_health x
               SET probe_until = v_now + make_interval(
                       secs => l.breaker_cooldown_seconds::double precision),
                   updated_at = v_now
             WHERE x.connector_id = o.connector_id
               AND x.provider_account_id = o.provider_account_id;
            INSERT INTO {s}.safeact_connector_health_events (
                connector_id, provider_account_id, event,
                consecutive_failures, operation_id
            ) VALUES (o.connector_id, o.provider_account_id,
                      'probe_admitted', h.consecutive_failures, p_operation);
        END IF;
    END IF;

    IF l.dispatch_rate_per_second IS NOT NULL THEN
        v_tokens := least(
            l.dispatch_burst::numeric,
            h.tokens + l.dispatch_rate_per_second
                * extract(epoch FROM (v_now - h.refilled_at))
        );
        IF v_tokens < 1 THEN
            RAISE EXCEPTION 'Provider dispatch rate reached'
                USING ERRCODE = {limited};
        END IF;
        UPDATE {s}.safeact_connector_health x
           SET tokens = v_tokens - 1, refilled_at = v_now, updated_at = v_now
         WHERE x.connector_id = o.connector_id
           AND x.provider_account_id = o.provider_account_id;
    END IF;
END;""", definer=False)


def _pacing(s):
    return _function(
        s, "safeact_pacing", "p_operation uuid",
        "TABLE(next_lookup_at timestamptz, retry_not_before timestamptz, "
        "breaker_open_until timestamptz, limited boolean, "
        "observed_at timestamptz)", """
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM {s}.safeact_operations o
        WHERE o.operation_id = p_operation
    ) THEN
        RAISE EXCEPTION 'Unknown operation' USING ERRCODE = {unknown};
    END IF;

    RETURN QUERY
    SELECT {s}.safeact_next_lookup_at(p_operation),
           (SELECT r.not_before FROM {s}.safeact_retry_schedule r
             WHERE r.operation_id = p_operation
             ORDER BY r.seq DESC LIMIT 1),
           (SELECT h.open_until FROM {s}.safeact_operations o
              JOIN {s}.safeact_connector_health h
                ON h.connector_id = o.connector_id
               AND h.provider_account_id = o.provider_account_id
             WHERE o.operation_id = p_operation),
           EXISTS (
               SELECT 1 FROM {s}.safeact_operations o
               JOIN {s}.safeact_contract_limits k
                 ON k.connector_id = o.connector_id
                AND k.version = o.contract_version
               WHERE o.operation_id = p_operation),
           clock_timestamp();
END;""", volatility="STABLE")


def _append(s):
    # The frozen 0017 append, plus three 0018 blocks: lookup pacing before
    # the transition insert; retry schedule and breaker after it.
    base = v0017._append(s).as_string(None)
    ss = s.as_string(None)
    insert = "    INSERT INTO " + ss + ".safeact_operation_transitions ("
    audit = "    INSERT INTO " + ss + ".safeact_audit_events ("
    if base.count(insert) != 1 or base.count(audit) != 1:
        raise RuntimeError("0017 append changed shape")
    head, rest = base.split(insert)
    middle, tail = rest.split(audit)

    pacing = sql.SQL("""    -- 0018: one reconciler lookup per paced slot (F16).
    IF p_from = 'UNKNOWN' AND p_to = 'VERIFYING'
       AND {s}.safeact_next_lookup_at(p_operation) > clock_timestamp() THEN
        RAISE EXCEPTION 'Next lookup not due before %',
            {s}.safeact_next_lookup_at(p_operation)
            USING ERRCODE = {not_due};
    END IF;

""").format(s=s, not_due=sql.Literal(NOT_YET_DUE)).as_string(None)

    after = sql.SQL("""    -- 0018: retry backoff, in the same transaction as the move.
    IF p_to = 'RETRY_ELIGIBLE' AND EXISTS (
        SELECT 1 FROM {s}.safeact_operations o
        JOIN {s}.safeact_contract_limits k
          ON k.connector_id = o.connector_id AND k.version = o.contract_version
        WHERE o.operation_id = p_operation
    ) THEN
        INSERT INTO {s}.safeact_retry_schedule (
            operation_id, seq, attempts, backoff_seconds,
            retry_after_seconds, not_before
        )
        SELECT p_operation, p_seq, n.attempts,
               {s}.safeact_backoff_seconds(p_operation, n.attempts),
               r.retry_after,
               clock_timestamp() + make_interval(secs => greatest(
                   {s}.safeact_backoff_seconds(p_operation, n.attempts),
                   coalesce(r.retry_after, 0))::double precision)
        FROM (SELECT greatest(count(*), 1)::integer AS attempts
              FROM {s}.safeact_attempts a
              WHERE a.operation_id = p_operation) n
        CROSS JOIN (
            SELECT max((x.details -> 'provider' ->> 'retry_after')::numeric)
                   AS retry_after
            FROM {s}.safeact_attempt_outcomes x
            WHERE x.operation_id = p_operation
              AND x.details -> 'provider' ->> 'retry_after'
                  ~ '^[0-9]{{1,6}}([.][0-9]{{1,3}})?$'
        ) r;
    END IF;

    -- 0018: the provider breaker follows dispatch outcomes.
    IF p_from = 'DISPATCHING' AND p_to IN ('UNKNOWN', 'SUCCEEDED',
                                         'FAILED_CONFIRMED') THEN
        DECLARE
            o record;
            l record;
            h record;
        BEGIN
            SELECT x.connector_id, x.contract_version, x.provider_account_id
            INTO o FROM {s}.safeact_operations x
            WHERE x.operation_id = p_operation;
            SELECT k.breaker_threshold, k.breaker_cooldown_seconds INTO l
            FROM {s}.safeact_contract_limits k
            WHERE k.connector_id = o.connector_id
              AND k.version = o.contract_version;
            IF FOUND AND l.breaker_threshold IS NOT NULL THEN
                INSERT INTO {s}.safeact_connector_health (
                    connector_id, provider_account_id, tokens, refilled_at
                ) VALUES (o.connector_id, o.provider_account_id, 0,
                          clock_timestamp())
                ON CONFLICT (connector_id, provider_account_id) DO NOTHING;
                SELECT * INTO h FROM {s}.safeact_connector_health x
                WHERE x.connector_id = o.connector_id
                  AND x.provider_account_id = o.provider_account_id
                FOR UPDATE;
                IF p_to = 'UNKNOWN' AND p_reason IN ({failing}) THEN
                    UPDATE {s}.safeact_connector_health x
                       SET consecutive_failures = h.consecutive_failures + 1,
                           probe_until = NULL,
                           open_until = CASE
                               WHEN h.consecutive_failures + 1
                                    >= l.breaker_threshold
                               THEN clock_timestamp() + make_interval(
                                   secs => l.breaker_cooldown_seconds
                                       ::double precision)
                               ELSE h.open_until END,
                           updated_at = clock_timestamp()
                     WHERE x.connector_id = o.connector_id
                       AND x.provider_account_id = o.provider_account_id;
                    IF h.consecutive_failures + 1 >= l.breaker_threshold THEN
                        INSERT INTO {s}.safeact_connector_health_events (
                            connector_id, provider_account_id, event,
                            consecutive_failures, operation_id, until
                        ) VALUES (
                            o.connector_id, o.provider_account_id,
                            'breaker_opened', h.consecutive_failures + 1,
                            p_operation, clock_timestamp() + make_interval(
                                secs => l.breaker_cooldown_seconds
                                    ::double precision));
                    END IF;
                ELSIF p_to IN ('SUCCEEDED', 'FAILED_CONFIRMED') THEN
                    IF h.consecutive_failures >= l.breaker_threshold THEN
                        INSERT INTO {s}.safeact_connector_health_events (
                            connector_id, provider_account_id, event,
                            consecutive_failures, operation_id
                        ) VALUES (
                            o.connector_id, o.provider_account_id,
                            'breaker_closed', h.consecutive_failures,
                            p_operation);
                    END IF;
                    UPDATE {s}.safeact_connector_health x
                       SET consecutive_failures = 0, open_until = NULL,
                           probe_until = NULL, updated_at = clock_timestamp()
                     WHERE x.connector_id = o.connector_id
                       AND x.provider_account_id = o.provider_account_id;
                END IF;
            END IF;
        END;
    END IF;

""").format(s=s, failing=_values(FAILING_REASONS)).as_string(None)

    return sql.SQL(head + pacing + insert + middle + after + audit + tail)


def _claim(s):
    # The frozen 0007 claim, plus the dispatch gate (backoff, breaker,
    # rate) right before it appends CLAIMED.
    base = v0007._claim(s).as_string(None)
    ss = s.as_string(None)
    marker = ("    PERFORM " + ss + ".safeact_lifecycle_append(\n"
              "        p_operation, v.o_seq + 1, v.o_state, 'CLAIMED'")
    if base.count(marker) != 1:
        raise RuntimeError("0007 claim changed shape")
    head, tail = base.split(marker)
    gate = sql.SQL("""    -- 0018: retry backoff, provider breaker and dispatch rate.
    -- Refused claims append nothing and spend no claim budget.
    PERFORM {s}.safeact_dispatch_gate(p_operation, v_retry);

""").format(s=s).as_string(None)
    return sql.SQL(head + gate + marker + tail)


def _dispatch_outcome(s):
    base = v0007._dispatch_outcome(s).as_string(None)
    old = sql.SQL("({})").format(_values(v0007.AMBIGUOUS_REASONS)).as_string(
        None)
    new = sql.SQL("({})").format(_values(
        set(v0007.AMBIGUOUS_REASONS) | {RATE_LIMITED_REASON}
    )).as_string(None)
    if base.count(old) != 1:
        raise RuntimeError("0007 dispatch outcome changed shape")
    return sql.SQL(base.replace(old, new))


# Claimable: never-claimed work, plus RETRY_ELIGIBLE work within the
# abandoned-claim cap whose retry is due, unless its provider's breaker
# is open. Same order as before. The token bucket is checked at claim.
_CLAIMABLE = """
    SELECT c.operation_id FROM (
        SELECT o.operation_id, o.created_at, o.connector_id,
               o.provider_account_id
        FROM {s}.{p} p
        JOIN {s}.safeact_operations o
          ON o.operation_id = p.operation_id
        WHERE p.state = 'RECORDED'
        UNION ALL
        SELECT o.operation_id, o.created_at, o.connector_id,
               o.provider_account_id
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
          AND NOT EXISTS (
              SELECT 1 FROM {s}.safeact_retry_schedule r
              WHERE r.operation_id = p.operation_id AND r.seq = p.seq
                AND r.not_before > clock_timestamp()
          )
    ) c
    WHERE NOT EXISTS (
        SELECT 1 FROM {s}.safeact_connector_health h
        WHERE h.connector_id = c.connector_id
          AND h.provider_account_id = c.provider_account_id
          AND h.open_until > clock_timestamp()
    )
    ORDER BY c.created_at, c.operation_id
    LIMIT {limit}"""


def claimable_query(schema, limit):
    return sql.SQL(_CLAIMABLE).format(
        s=v0003._schema(schema),
        p=sql.Identifier(v0017.PROJECTION_TABLE),
        max_abandoned=sql.Literal(v0007.MAX_ABANDONED_CLAIMS), limit=limit,
    )


def _scan_function(s, name, args, returns, body):
    header = (
        "CREATE OR REPLACE FUNCTION {s}.{name}(" + args + ") RETURNS "
        + returns + " STABLE " + _HEADER.format(security="SECURITY DEFINER")
    )
    return sql.SQL(header + body + " $safeact_lifecycle$").format(
        s=s, name=sql.Identifier(name),
        p=sql.Identifier(v0017.PROJECTION_TABLE),
        max_page=sql.Literal(v0003.MAX_PAGE),
        max_abandoned=sql.Literal(v0007.MAX_ABANDONED_CLAIMS),
    )


def _claimable_function(s):
    return _scan_function(
        s, "safeact_claimable_operations", "p_limit integer",
        "TABLE(operation_id uuid)", """
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND {max_page} THEN
        RAISE EXCEPTION 'Invalid page size' USING ERRCODE = '22023';
    END IF;

    RETURN QUERY """ + _CLAIMABLE.replace("{limit}", "p_limit") + """;
END;""")


def _relay(s):
    return _scan_function(
        s, "safeact_relay_pending", "p_limit integer",
        "TABLE(outbox_id uuid, operation_id uuid)", """
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND {max_page} THEN
        RAISE EXCEPTION 'Invalid page size' USING ERRCODE = '22023';
    END IF;

    -- Claimable work, including due reclaimable retries. The relay may
    -- publish one more than once; the claim makes that harmless.
    RETURN QUERY
    SELECT e.outbox_id, e.operation_id
    FROM (""" + _CLAIMABLE.replace("{limit}", "p_limit") + """) c
    JOIN {s}.safeact_outbox_events e
      ON e.operation_id = c.operation_id
     AND e.event_type = 'INTENT_RECORDED_NO_DISPATCH'
     AND e.state = 'PENDING';
END;""")


def _signature(s, name, arguments):
    return sql.SQL("{}.{}({})").format(
        s, sql.Identifier(name), sql.SQL(arguments)
    )


def upgrade_statements(schema):
    s = v0003._schema(schema)
    return _tables(s) + [
        _backoff(s), _next_lookup(s), _gate(s), _pacing(s),
        _append(s), _claim(s), _dispatch_outcome(s),
        _claimable_function(s), _relay(s),
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
    return [
        v0017._append(s),
        v0007._claim(s),
        v0007._dispatch_outcome(s),
        v0017._claimable_function(s),
        v0017._relay(s),
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
