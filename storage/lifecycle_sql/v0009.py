"""FROZEN once committed: SQL for revision 0009_workflow.

The fixed two-step workflow (replacement_shipping_v1): charge a
shipping fee (payments), then create a replacement order (CRM).

- safeact_workflows records the whole plan at creation: the order
  step's identity and canonical payload, and the compensation policy.
  The charge is admitted in the same transaction (Python, through the
  normal admission path).
- safeact_workflow_steps links each step to its own operation. A step
  is admitted at most once (primary key); an operation belongs to at
  most one step.
- safeact_admit_workflow_step admits the order only after the charge
  SUCCEEDED (SAT15), from the stored plan, never from caller input.
  It is idempotent: a repeated or ack-lost advance returns the
  operation already admitted. The provider key is derived in SQL, the
  same derivation the 0008 CHECK enforces.
- Workflow status is derived from the step operations' states
  (safeact_workflow_status_of, mirrored by core.workflow); it is not a
  second state machine.

Pinned by tests/phase_a/test_migration_sql_frozen.py. Values are
literals; nothing is read from core.
"""

from psycopg import sql

from storage.lifecycle_sql import v0003, v0008


REVISION = "0009_workflow"

WORKFLOW_PRECONDITION = "SAT15"
WORKFLOW_TYPE = "replacement_shipping_v1"
STATUS_VIEW = "safeact_workflow_status"

NEW_TABLES = ("safeact_workflows", "safeact_workflow_steps")
NEW_VIEWS = (STATUS_VIEW,)
HISTORY_TABLES = ("safeact_workflow_steps", "safeact_workflows")
LEGAL_TRANSITIONS = frozenset()

NEW_FUNCTIONS = {
    "safeact_workflow_status_of": "text, text, text",
    "safeact_admit_workflow_step": "uuid, text",
    "safeact_workflows_to_advance": "integer",
}
REPLACED_FUNCTIONS = ()

SHARED_FUNCTIONS = {}
WORKER_FUNCTIONS = {}
RECOVERY_FUNCTIONS = {}
INTERNAL_FUNCTIONS = {"safeact_workflow_status_of": "text, text, text"}
ADVANCER_FUNCTIONS = {
    "safeact_admit_workflow_step": "uuid, text",
    "safeact_workflows_to_advance": "integer",
}
# Full privilege sets (the head merge replaces per table).
OWNER_TABLE_PRIVILEGES = {
    "safeact_operations": ("SELECT", "INSERT"),
    "safeact_audit_events": ("INSERT",),
    "safeact_outbox_events": ("SELECT", "INSERT"),
    "safeact_workflows": ("SELECT",),
    "safeact_workflow_steps": ("SELECT", "INSERT"),
}

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
        precondition=sql.Literal(WORKFLOW_PRECONDITION),
        max_page=sql.Literal(v0003.MAX_PAGE),
        workflow_type=sql.Literal(WORKFLOW_TYPE),
        view=sql.Identifier(v0003.CURRENT_STATE_VIEW),
        derived_key=sql.SQL(v0008.DERIVED_KEY.replace(
            "tenant_id", "v_wf.tenant_id"
        ).replace(
            "request_key", "v_request_key"
        ).replace(
            "connector_id", "v_step.connector_id"
        ).replace(
            "provider_account_id", "v_step.provider_account_id"
        )),
    )


def _tables(s):
    return [
        sql.SQL("""
CREATE TABLE {s}.safeact_workflows (
    workflow_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    request_key varchar(255) NOT NULL,
    workflow_type varchar(64) NOT NULL,
    created_by varchar(128) NOT NULL,
    plan jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_safeact_workflows_tenant_request
        UNIQUE (tenant_id, request_key),
    CONSTRAINT ck_safeact_workflows_type
        CHECK (workflow_type = {workflow_type}),
    CONSTRAINT ck_safeact_workflows_text
        CHECK (tenant_id <> '' AND request_key <> ''
               AND created_by <> ''),
    CONSTRAINT ck_safeact_workflows_plan
        CHECK (jsonb_typeof(plan) = 'object'
               AND jsonb_typeof(plan -> 'order') = 'object'
               AND jsonb_typeof(plan -> 'compensation') = 'object')
)""").format(s=s, workflow_type=sql.Literal(WORKFLOW_TYPE)),

        sql.SQL("""
CREATE TABLE {s}.safeact_workflow_steps (
    workflow_id uuid NOT NULL
        REFERENCES {s}.safeact_workflows (workflow_id)
        ON DELETE RESTRICT,
    step varchar(16) NOT NULL,
    operation_id uuid NOT NULL
        REFERENCES {s}.safeact_operations (operation_id)
        ON DELETE RESTRICT,
    compensates_operation_id uuid
        REFERENCES {s}.safeact_operations (operation_id)
        ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_safeact_workflow_steps PRIMARY KEY (workflow_id, step),
    CONSTRAINT uq_safeact_workflow_steps_operation UNIQUE (operation_id),
    CONSTRAINT uq_safeact_workflow_steps_compensates
        UNIQUE (compensates_operation_id),
    CONSTRAINT ck_safeact_workflow_steps_step
        CHECK (step IN ('charge', 'compensation', 'order')),
    CONSTRAINT ck_safeact_workflow_steps_compensates
        CHECK ((step = 'compensation')
               = (compensates_operation_id IS NOT NULL))
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


def _status_of(s):
    return _function(
        s, "safeact_workflow_status_of",
        "p_charge text, p_order text, p_compensation text", "text", """
DECLARE
    v_in_flight text[] := ARRAY['RECORDED', 'CLAIMED', 'DISPATCHING',
                                'UNKNOWN', 'VERIFYING', 'RETRY_ELIGIBLE'];
BEGIN
    IF (p_order IS NOT NULL AND p_charge IS DISTINCT FROM 'SUCCEEDED')
       OR (p_compensation IS NOT NULL
           AND p_order IS DISTINCT FROM 'FAILED_CONFIRMED') THEN
        RETURN 'INVALID';
    END IF;

    IF p_charge = ANY (v_in_flight) THEN
        RETURN 'IN_PROGRESS';
    ELSIF p_charge = 'FAILED_CONFIRMED' THEN
        RETURN 'FAILED_NO_EFFECT';
    ELSIF p_charge = 'ESCALATED_UNRESOLVED' THEN
        RETURN 'UNRESOLVED';
    ELSIF p_charge IS DISTINCT FROM 'SUCCEEDED' THEN
        RETURN 'INVALID';
    END IF;

    IF p_order IS NULL OR p_order = ANY (v_in_flight) THEN
        RETURN 'IN_PROGRESS';
    ELSIF p_order = 'SUCCEEDED' THEN
        RETURN 'COMPLETED';
    ELSIF p_order = 'ESCALATED_UNRESOLVED' THEN
        RETURN 'UNRESOLVED';
    END IF;

    IF p_compensation IS NULL OR p_compensation = ANY (v_in_flight) THEN
        RETURN 'COMPENSATING';
    ELSIF p_compensation = 'SUCCEEDED' THEN
        RETURN 'COMPENSATED_VERIFIED';
    END IF;
    RETURN 'COMPENSATION_UNRESOLVED';
END;""", definer=False, volatility="IMMUTABLE")


def _view(s):
    return sql.SQL("""
CREATE VIEW {s}.{view} AS
SELECT
    w.workflow_id,
    w.tenant_id,
    w.request_key,
    charge.operation_id AS charge_operation_id,
    charge_state.state AS charge_state,
    ord.operation_id AS order_operation_id,
    order_state.state AS order_state,
    comp.operation_id AS compensation_operation_id,
    comp_state.state AS compensation_state,
    {s}.safeact_workflow_status_of(
        charge_state.state, order_state.state, comp_state.state
    ) AS status
FROM {s}.safeact_workflows w
LEFT JOIN {s}.safeact_workflow_steps charge
  ON charge.workflow_id = w.workflow_id AND charge.step = 'charge'
LEFT JOIN {s}.{current} charge_state
  ON charge_state.operation_id = charge.operation_id
LEFT JOIN {s}.safeact_workflow_steps ord
  ON ord.workflow_id = w.workflow_id AND ord.step = 'order'
LEFT JOIN {s}.{current} order_state
  ON order_state.operation_id = ord.operation_id
LEFT JOIN {s}.safeact_workflow_steps comp
  ON comp.workflow_id = w.workflow_id AND comp.step = 'compensation'
LEFT JOIN {s}.{current} comp_state
  ON comp_state.operation_id = comp.operation_id
""").format(
        s=s, view=sql.Identifier(STATUS_VIEW),
        current=sql.Identifier(v0003.CURRENT_STATE_VIEW),
    )


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
END;""")


def _to_advance(s):
    return _function(
        s, "safeact_workflows_to_advance", "p_limit integer",
        "TABLE(workflow_id uuid, step text)", """
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND {max_page} THEN
        RAISE EXCEPTION 'Invalid page size' USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    SELECT st.workflow_id, 'order'::text
    FROM {s}.safeact_workflow_steps st
    JOIN {s}.{view} c ON c.operation_id = st.operation_id
    WHERE st.step = 'charge' AND c.state = 'SUCCEEDED'
      AND NOT EXISTS (
          SELECT 1 FROM {s}.safeact_workflow_steps o
          WHERE o.workflow_id = st.workflow_id AND o.step = 'order'
      )
    ORDER BY st.workflow_id
    LIMIT p_limit;
END;""", volatility="STABLE")


def _signature(s, name, arguments):
    return sql.SQL("{}.{}({})").format(
        s, sql.Identifier(name), sql.SQL(arguments)
    )


def upgrade_statements(schema):
    s = v0003._schema(schema)
    return _tables(s) + [
        _status_of(s),
        _view(s),
        _admit_step(s),
        _to_advance(s),
    ] + [
        sql.SQL("REVOKE ALL ON TABLE {}.{} FROM PUBLIC").format(
            s, sql.Identifier(name)
        )
        for name in NEW_TABLES + NEW_VIEWS
    ] + [
        sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(
            _signature(s, name, arguments)
        )
        for name, arguments in NEW_FUNCTIONS.items()
    ]


def downgrade_statements(schema):
    s = v0003._schema(schema)
    return [
        sql.SQL("DROP FUNCTION IF EXISTS {}").format(
            _signature(s, name, arguments)
        )
        for name, arguments in reversed(tuple(NEW_FUNCTIONS.items()))
        if name != "safeact_workflow_status_of"
    ] + [
        sql.SQL("DROP VIEW IF EXISTS {}.{}").format(
            s, sql.Identifier(STATUS_VIEW)
        ),
        sql.SQL("DROP FUNCTION IF EXISTS {}").format(_signature(
            s, "safeact_workflow_status_of",
            NEW_FUNCTIONS["safeact_workflow_status_of"],
        )),
    ] + [
        sql.SQL("DROP TABLE IF EXISTS {}.{}").format(
            s, sql.Identifier(table)
        )
        for table in reversed(NEW_TABLES)
    ]
