"""FROZEN once committed: SQL for revision 0010_compensation.

Safe compensation for the fixed workflow.

- The compensation is a separately identified operation (a refund of
  exactly the charge), linked by compensates_operation_id, which is
  unique: at most one inverse per forward effect. The forward history
  is never altered.
- It is admitted only when the charge SUCCEEDED with exactly one
  applied effect reference and the order is FAILED_CONFIRMED, and only
  under the compensation policy recorded at creation (SAT16
  otherwise). Never on an UNKNOWN or escalated order: the order may
  exist.
- Its payload is derived in SQL from the durable charge record and its
  applied reference (amount and charge id), never from caller input.
- The inverse is verified by its own lifecycle: the workflow shows
  COMPENSATED_VERIFIED only when the refund operation SUCCEEDED with
  applied evidence.

Pinned by tests/phase_a/test_migration_sql_frozen.py. Values are
literals; nothing is read from core.
"""

from psycopg import sql

from storage.lifecycle_sql import v0003, v0008, v0009


REVISION = "0010_compensation"

COMPENSATION_PRECONDITION = "SAT16"
POLICY = "refund_charge_when_order_failed_confirmed"

NEW_TABLES = ()
HISTORY_TABLES = ()
LEGAL_TRANSITIONS = frozenset()

NEW_FUNCTIONS = {"safeact_admit_compensation": "uuid"}
REPLACED_FUNCTIONS = (
    "safeact_admit_workflow_step",
    "safeact_workflows_to_advance",
)

SHARED_FUNCTIONS = {}
WORKER_FUNCTIONS = {}
RECOVERY_FUNCTIONS = {}
INTERNAL_FUNCTIONS = {"safeact_admit_compensation": "uuid"}
ADVANCER_FUNCTIONS = {}
OWNER_TABLE_PRIVILEGES = {}

_HEADER = (
    "LANGUAGE plpgsql {security} "
    "SET search_path = pg_catalog, pg_temp "
    "AS $safeact_lifecycle$ "
)


def _function(s, name, args, returns, body, *, verb="CREATE",
              definer=True, volatility=""):
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
        unknown=sql.Literal(v0003.UNKNOWN_OPERATION),
        precondition=sql.Literal(v0009.WORKFLOW_PRECONDITION),
        compensation=sql.Literal(COMPENSATION_PRECONDITION),
        policy=sql.Literal(POLICY),
        max_page=sql.Literal(v0003.MAX_PAGE),
        view=sql.Identifier(v0003.CURRENT_STATE_VIEW),
        derived_key=_key("v_wf", "v_step"),
        comp_key=_key("v_charge", "v_comp"),
    )


def _key(identity, step):
    # The 0008 derivation over the function's own variables.
    return sql.SQL(v0008.DERIVED_KEY.replace(
        "tenant_id", identity + ".tenant_id"
    ).replace(
        "request_key", "v_request_key"
    ).replace(
        "connector_id", step + ".connector_id"
    ).replace(
        "provider_account_id", step + ".provider_account_id"
    ))


def _admit_compensation(s):
    return _function(
        s, "safeact_admit_compensation", "p_workflow uuid", "uuid", """
DECLARE
    v_plan jsonb;
    v_charge record;
    v_comp record;
    v_refs text[];
    v_amount bigint;
    v_payload text;
    v_request_key text;
    v_key text;
    v_operation uuid := gen_random_uuid();
BEGIN
    -- Called from safeact_admit_workflow_step, under its workflow
    -- lock and after its idempotency check.
    SELECT w.plan INTO v_plan
    FROM {s}.safeact_workflows w WHERE w.workflow_id = p_workflow;

    IF v_plan -> 'compensation' ->> 'policy' IS DISTINCT FROM {policy}
    THEN
        RAISE EXCEPTION 'No authorized compensation policy'
            USING ERRCODE = {compensation};
    END IF;

    -- Only after a confirmed order failure: never on UNKNOWN or an
    -- escalated order, which may exist.
    PERFORM 1
    FROM {s}.safeact_workflow_steps st
    JOIN {s}.{view} c ON c.operation_id = st.operation_id
    WHERE st.workflow_id = p_workflow AND st.step = 'order'
      AND c.state = 'FAILED_CONFIRMED';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Compensation needs a confirmed order failure'
            USING ERRCODE = {compensation};
    END IF;

    SELECT o.operation_id, o.tenant_id, o.resource_id,
           o.payload_canonical
    INTO v_charge
    FROM {s}.safeact_workflow_steps st
    JOIN {s}.safeact_operations o ON o.operation_id = st.operation_id
    JOIN {s}.{view} c ON c.operation_id = st.operation_id
    WHERE st.workflow_id = p_workflow AND st.step = 'charge'
      AND c.state = 'SUCCEEDED';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Compensation needs a succeeded charge'
            USING ERRCODE = {compensation};
    END IF;

    -- The inverse names exactly the applied effect: one reference.
    SELECT array_agg(DISTINCT x.evidence_reference) INTO v_refs
    FROM {s}.safeact_attempt_outcomes x
    WHERE x.operation_id = v_charge.operation_id
      AND x.outcome = 'confirmed_applied'
      AND x.evidence_kind = 'applied'
      AND x.evidence_reference IS NOT NULL;

    IF v_refs IS NULL OR cardinality(v_refs) <> 1 THEN
        RAISE EXCEPTION 'Charge effect reference is missing or ambiguous'
            USING ERRCODE = {compensation};
    END IF;

    IF jsonb_typeof(v_charge.payload_canonical::jsonb -> 'amount_minor')
       IS DISTINCT FROM 'number' THEN
        RAISE EXCEPTION 'Charge amount is not recorded'
            USING ERRCODE = {compensation};
    END IF;
    v_amount := (v_charge.payload_canonical::jsonb ->> 'amount_minor')
                ::bigint;

    -- Canonical form (sorted keys, no spaces), built from the durable
    -- forward record, never from caller input.
    v_payload := '{{"amount_minor":' || v_amount::text
                 || ',"charge_id":' || to_json(v_refs[1])::text || '}}';

    SELECT p.action_type, p.connector_id, p.provider_account_id,
           p.contract_version
    INTO v_comp
    FROM jsonb_to_record(v_plan -> 'compensation') AS p(
        action_type text, connector_id text, provider_account_id text,
        contract_version text
    );

    v_request_key := 'wf:' || p_workflow::text || ':compensation';
    v_key := CASE WHEN EXISTS (
        SELECT 1 FROM {s}.safeact_connector_contracts k
        WHERE k.connector_id = v_comp.connector_id
          AND k.version = v_comp.contract_version
          AND k.native_idempotency
    ) THEN {comp_key} END;

    INSERT INTO {s}.safeact_operations (
        operation_id, tenant_id, request_key, action_type, resource_id,
        provider_account_id, connector_id, contract_version,
        payload_sha256, payload_canonical, provider_idempotency_key
    ) VALUES (
        v_operation, v_charge.tenant_id, v_request_key,
        v_comp.action_type, v_charge.resource_id,
        v_comp.provider_account_id, v_comp.connector_id,
        v_comp.contract_version,
        encode(sha256(convert_to(v_payload, 'UTF8')), 'hex'),
        v_payload, v_key
    );

    INSERT INTO {s}.safeact_audit_events (
        operation_id, event_type, actor_id, details
    ) VALUES (
        v_operation, 'INTENT_RECORDED', session_user::text,
        jsonb_build_object(
            'schema_version', 1,
            'dispatch_authorized', false,
            'workflow_id', p_workflow,
            'step', 'compensation',
            'compensates_operation_id', v_charge.operation_id,
            'compensates_reference', v_refs[1]
        )
    );

    INSERT INTO {s}.safeact_outbox_events (
        outbox_id, operation_id, event_type, payload
    ) VALUES (
        gen_random_uuid(), v_operation, 'INTENT_RECORDED_NO_DISPATCH',
        jsonb_build_object('dispatch_enabled', false)
    );

    INSERT INTO {s}.safeact_workflow_steps (
        workflow_id, step, operation_id, compensates_operation_id
    ) VALUES (
        p_workflow, 'compensation', v_operation, v_charge.operation_id
    );

    RETURN v_operation;
END;""", definer=False)


def _admit_step(s):
    return _function(
        s, "safeact_admit_workflow_step", "p_workflow uuid, p_step text",
        "uuid", """
DECLARE
    v_wf record;
    v_step record;
    v_existing uuid;
    v_charge uuid;
    v_operation uuid := gen_random_uuid();
    v_request_key text;
    v_key text;
BEGIN
    -- Serialize advancement of one workflow.
    PERFORM pg_advisory_xact_lock(
        hashtextextended('safeact_workflow:' || p_workflow::text, 0)
    );

    SELECT w.workflow_id, w.tenant_id, w.plan INTO v_wf
    FROM {s}.safeact_workflows w
    WHERE w.workflow_id = p_workflow;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Unknown workflow' USING ERRCODE = {unknown};
    END IF;

    -- Idempotent: a step is admitted at most once.
    SELECT st.operation_id INTO v_existing
    FROM {s}.safeact_workflow_steps st
    WHERE st.workflow_id = p_workflow AND st.step = p_step;

    IF FOUND THEN
        RETURN v_existing;
    END IF;

    IF p_step = 'compensation' THEN
        RETURN {s}.safeact_admit_compensation(p_workflow);
    END IF;

    IF p_step IS DISTINCT FROM 'order' THEN
        RAISE EXCEPTION 'Step % cannot be admitted here', p_step
            USING ERRCODE = {precondition};
    END IF;

    SELECT st.operation_id INTO v_charge
    FROM {s}.safeact_workflow_steps st
    JOIN {s}.{view} c ON c.operation_id = st.operation_id
    WHERE st.workflow_id = p_workflow AND st.step = 'charge'
      AND c.state = 'SUCCEEDED';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'The order waits for a successful charge'
            USING ERRCODE = {precondition};
    END IF;

    -- The order comes from the plan recorded at creation.
    SELECT p.action_type, p.resource_id, p.provider_account_id,
           p.connector_id, p.contract_version, p.payload_canonical
    INTO v_step
    FROM jsonb_to_record(v_wf.plan -> 'order') AS p(
        action_type text, resource_id text, provider_account_id text,
        connector_id text, contract_version text,
        payload_canonical text
    );

    v_request_key := 'wf:' || p_workflow::text || ':order';
    v_key := CASE WHEN EXISTS (
        SELECT 1 FROM {s}.safeact_connector_contracts k
        WHERE k.connector_id = v_step.connector_id
          AND k.version = v_step.contract_version
          AND k.native_idempotency
    ) THEN {derived_key} END;

    INSERT INTO {s}.safeact_operations (
        operation_id, tenant_id, request_key, action_type, resource_id,
        provider_account_id, connector_id, contract_version,
        payload_sha256, payload_canonical, provider_idempotency_key
    ) VALUES (
        v_operation, v_wf.tenant_id, v_request_key, v_step.action_type,
        v_step.resource_id, v_step.provider_account_id,
        v_step.connector_id, v_step.contract_version,
        encode(sha256(convert_to(v_step.payload_canonical, 'UTF8')),
               'hex'),
        v_step.payload_canonical, v_key
    );

    INSERT INTO {s}.safeact_audit_events (
        operation_id, event_type, actor_id, details
    ) VALUES (
        v_operation, 'INTENT_RECORDED', session_user::text,
        jsonb_build_object(
            'schema_version', 1,
            'dispatch_authorized', false,
            'workflow_id', p_workflow,
            'step', 'order'
        )
    );

    INSERT INTO {s}.safeact_outbox_events (
        outbox_id, operation_id, event_type, payload
    ) VALUES (
        gen_random_uuid(), v_operation, 'INTENT_RECORDED_NO_DISPATCH',
        jsonb_build_object('dispatch_enabled', false)
    );

    INSERT INTO {s}.safeact_workflow_steps (workflow_id, step, operation_id)
    VALUES (p_workflow, 'order', v_operation);

    RETURN v_operation;
END;""", verb="CREATE OR REPLACE")


def _to_advance(s):
    return _function(
        s, "safeact_workflows_to_advance", "p_limit integer",
        "TABLE(workflow_id uuid, step text)", """
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND {max_page} THEN
        RAISE EXCEPTION 'Invalid page size' USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    SELECT r.workflow_id, r.step FROM (
        SELECT st.workflow_id, 'order'::text AS step
        FROM {s}.safeact_workflow_steps st
        JOIN {s}.{view} c ON c.operation_id = st.operation_id
        WHERE st.step = 'charge' AND c.state = 'SUCCEEDED'
          AND NOT EXISTS (
              SELECT 1 FROM {s}.safeact_workflow_steps o
              WHERE o.workflow_id = st.workflow_id AND o.step = 'order'
          )
        UNION ALL
        SELECT st.workflow_id, 'compensation'::text
        FROM {s}.safeact_workflow_steps st
        JOIN {s}.{view} c ON c.operation_id = st.operation_id
        WHERE st.step = 'order' AND c.state = 'FAILED_CONFIRMED'
          AND NOT EXISTS (
              SELECT 1 FROM {s}.safeact_workflow_steps k
              WHERE k.workflow_id = st.workflow_id
                AND k.step = 'compensation'
          )
    ) r
    ORDER BY r.workflow_id, r.step
    LIMIT p_limit;
END;""", verb="CREATE OR REPLACE", volatility="STABLE")


def _signature(s, name, arguments):
    return sql.SQL("{}.{}({})").format(
        s, sql.Identifier(name), sql.SQL(arguments)
    )


def _replace(statement):
    text = statement.as_string(None)
    if not text.startswith("CREATE FUNCTION "):
        raise ValueError("not a function definition")
    return sql.SQL("CREATE OR REPLACE" + text[len("CREATE"):])


def upgrade_statements(schema):
    s = v0003._schema(schema)
    return [
        _admit_compensation(s),
        _admit_step(s),
        _to_advance(s),
    ] + [
        sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(
            _signature(s, name, arguments)
        )
        for name, arguments in NEW_FUNCTIONS.items()
    ]


def downgrade_statements(schema):
    s = v0003._schema(schema)
    return [
        _replace(v0009._admit_step(s)),
        _replace(v0009._to_advance(s)),
    ] + [
        sql.SQL("DROP FUNCTION IF EXISTS {}").format(
            _signature(s, name, arguments)
        )
        for name, arguments in NEW_FUNCTIONS.items()
    ]
