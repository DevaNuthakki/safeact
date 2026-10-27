"""FROZEN once committed: SQL for revision 0004_connector_contracts.

Adds versioned, immutable connector contracts and makes the dispatch
fence refuse any operation whose (connector_id, contract_version) has
no registered contract (SQLSTATE SAT10). The public schema registers
no contract, so the deny-only production boundary also holds here.

Pinned byte for byte by tests/phase_a/test_migration_sql_frozen.py.
Values are literals; nothing is read from core.
"""

from psycopg import sql

from storage.lifecycle_sql import v0003


REVISION = "0004_connector_contracts"

CONTRACT_UNKNOWN = "SAT10"

NEW_TABLES = ("safeact_connector_contracts",)
HISTORY_TABLES = ()
LEGAL_TRANSITIONS = frozenset()

NEW_FUNCTIONS = {
    "safeact_connector_contract": "text, text",
}
REPLACED_FUNCTIONS = ("safeact_begin_dispatch",)

# Disposable-only wiring additions for the head grants builder.
SHARED_FUNCTIONS = {"safeact_connector_contract": "text, text"}
WORKER_FUNCTIONS = {}
RECOVERY_FUNCTIONS = {}
OWNER_TABLE_PRIVILEGES = {"safeact_connector_contracts": ("SELECT",)}

SIDE_EFFECT_CLASSES = (
    "crm_record", "irreversible_message", "reversible_payment",
)
LOOKUP_SEMANTICS = ("eventual", "strong", "unavailable")

_HEADER = (
    "LANGUAGE plpgsql {security} "
    "SET search_path = pg_catalog, pg_temp "
    "AS $safeact_lifecycle$ "
)


def _values(values):
    return sql.SQL(", ").join(sql.Literal(v) for v in sorted(values))


def _function(s, name, args, returns, body, *, verb, stable=False):
    header = (
        verb + " FUNCTION {s}.{name}(" + args + ") RETURNS "
        + returns + " " + ("STABLE " if stable else "")
        + _HEADER.format(security="SECURITY DEFINER")
    )
    return sql.SQL(header + body + " $safeact_lifecycle$").format(
        s=s,
        name=sql.Identifier(name),
        illegal=sql.Literal(v0003.ILLEGAL_TRANSITION),
        stale=sql.Literal(v0003.STALE_TRANSITION),
        not_owner=sql.Literal(v0003.NOT_CLAIM_OWNER),
        lease_expired=sql.Literal(v0003.CLAIM_LEASE_EXPIRED),
        contract_unknown=sql.Literal(CONTRACT_UNKNOWN),
        max_lease=sql.Literal(v0003.MAX_LEASE_SECONDS),
    )


def _table(s):
    return [
        sql.SQL("""
CREATE TABLE {s}.safeact_connector_contracts (
    connector_id varchar(128) NOT NULL,
    version varchar(64) NOT NULL,
    side_effect_class varchar(32) NOT NULL,
    native_idempotency boolean NOT NULL,
    key_retention_seconds integer,
    key_safety_margin_seconds integer,
    key_binds_parameters boolean,
    key_protects_concurrent boolean,
    lookup_semantics varchar(16) NOT NULL,
    request_deadline_seconds integer NOT NULL,
    investigation_window_seconds integer NOT NULL,
    post_escalation_check_interval_seconds integer NOT NULL,
    post_escalation_horizon_seconds integer NOT NULL,
    max_attempts integer NOT NULL,
    max_reopens integer NOT NULL,
    compensation_action varchar(128),
    contract_reference varchar(255) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_safeact_connector_contracts
        PRIMARY KEY (connector_id, version),
    CONSTRAINT ck_safeact_connector_contracts_text
        CHECK (connector_id <> '' AND version <> ''
               AND contract_reference <> ''
               AND (compensation_action IS NULL
                    OR compensation_action <> '')),
    CONSTRAINT ck_safeact_connector_contracts_class
        CHECK (side_effect_class IN ({classes})),
    CONSTRAINT ck_safeact_connector_contracts_lookup
        CHECK (lookup_semantics IN ({lookups})),
    CONSTRAINT ck_safeact_connector_contracts_native
        CHECK ((native_idempotency
                AND key_retention_seconds IS NOT NULL
                AND key_safety_margin_seconds IS NOT NULL
                AND key_binds_parameters IS NOT NULL
                AND key_protects_concurrent IS NOT NULL)
            OR (NOT native_idempotency
                AND key_retention_seconds IS NULL
                AND key_safety_margin_seconds IS NULL
                AND key_binds_parameters IS NULL
                AND key_protects_concurrent IS NULL)),
    CONSTRAINT ck_safeact_connector_contracts_retention
        CHECK (NOT native_idempotency
               OR (key_retention_seconds > 0
                   AND key_safety_margin_seconds >= 0
                   AND key_safety_margin_seconds
                       < key_retention_seconds)),
    -- A request that passes the fence just before the retention
    -- cutoff must be sent while the provider still holds the key.
    CONSTRAINT ck_safeact_connector_contracts_deadline_margin
        CHECK (NOT native_idempotency
               OR key_safety_margin_seconds
                  >= request_deadline_seconds),
    CONSTRAINT ck_safeact_connector_contracts_timing
        CHECK (request_deadline_seconds > 0
               AND investigation_window_seconds >= 0
               AND post_escalation_check_interval_seconds > 0
               AND post_escalation_horizon_seconds >= 0),
    CONSTRAINT ck_safeact_connector_contracts_budgets
        CHECK (max_attempts >= 1 AND max_reopens >= 0)
)""").format(
            s=s,
            classes=_values(SIDE_EFFECT_CLASSES),
            lookups=_values(LOOKUP_SEMANTICS),
        ),
        sql.SQL(
            "CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE "
            "ON {s}.{table} FOR EACH ROW "
            "EXECUTE FUNCTION public.safeact_reject_mutation()"
        ).format(
            s=s,
            table=sql.Identifier("safeact_connector_contracts"),
            trigger=sql.Identifier(
                "safeact_connector_contracts_write_guard"
            ),
        ),
    ]


_CONTRACT_COLUMNS = """
           c.connector_id::text, c.version::text,
           c.side_effect_class::text, c.native_idempotency,
           c.key_retention_seconds, c.key_safety_margin_seconds,
           c.key_binds_parameters, c.key_protects_concurrent,
           c.lookup_semantics::text, c.request_deadline_seconds,
           c.investigation_window_seconds,
           c.post_escalation_check_interval_seconds,
           c.post_escalation_horizon_seconds, c.max_attempts,
           c.max_reopens, c.compensation_action::text,
           c.contract_reference::text"""


def _contract_function(s):
    return _function(
        s, "safeact_connector_contract",
        "p_connector text, p_version text",
        "TABLE(connector_id text, version text, side_effect_class text, "
        "native_idempotency boolean, key_retention_seconds integer, "
        "key_safety_margin_seconds integer, key_binds_parameters boolean, "
        "key_protects_concurrent boolean, lookup_semantics text, "
        "request_deadline_seconds integer, "
        "investigation_window_seconds integer, "
        "post_escalation_check_interval_seconds integer, "
        "post_escalation_horizon_seconds integer, max_attempts integer, "
        "max_reopens integer, compensation_action text, "
        "contract_reference text)", """
BEGIN
    RETURN QUERY
    SELECT""" + _CONTRACT_COLUMNS + """
    FROM {s}.safeact_connector_contracts c
    WHERE c.connector_id = p_connector AND c.version = p_version;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Unknown connector contract'
            USING ERRCODE = {contract_unknown};
    END IF;
END;""", verb="CREATE", stable=True)


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


def _signature(s, name, arguments):
    return sql.SQL("{}.{}({})").format(
        s, sql.Identifier(name), sql.SQL(arguments)
    )


def _replace(statement):
    """Turn a frozen earlier CREATE FUNCTION into CREATE OR REPLACE."""

    text = statement.as_string(None)
    if not text.startswith("CREATE FUNCTION "):
        raise ValueError("not a function definition")
    return sql.SQL("CREATE OR REPLACE" + text[len("CREATE"):])


def upgrade_statements(schema):
    s = v0003._schema(schema)
    return _table(s) + [
        _contract_function(s),
        _begin_dispatch(s),
    ] + [
        sql.SQL("REVOKE ALL ON TABLE {}.{} FROM PUBLIC").format(
            s, sql.Identifier("safeact_connector_contracts")
        ),
    ] + [
        sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(
            _signature(s, name, arguments)
        )
        for name, arguments in NEW_FUNCTIONS.items()
    ]


def downgrade_statements(schema):
    s = v0003._schema(schema)
    previous = {}
    for statement in v0003._functions(s):
        for name in REPLACED_FUNCTIONS:
            if f'."{name}"(' in statement.as_string(None)[:200]:
                previous[name] = statement
    if set(previous) != set(REPLACED_FUNCTIONS):
        raise RuntimeError("earlier function body not found")
    return [
        _replace(previous[name]) for name in REPLACED_FUNCTIONS
    ] + [
        sql.SQL("DROP FUNCTION IF EXISTS {}").format(
            _signature(s, name, arguments)
        )
        for name, arguments in NEW_FUNCTIONS.items()
    ] + [
        sql.SQL("DROP TABLE IF EXISTS {s}.{table}").format(
            s=s, table=sql.Identifier(table)
        )
        for table in NEW_TABLES
    ]
