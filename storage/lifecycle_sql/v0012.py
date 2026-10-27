"""FROZEN once committed: SQL for revision 0012_product_admission.

Authorized submission, workflow creation, tenant-scoped reads and
cancellation through the API role (Milestone 3).

- safeact_api_submit admits one action for the signed-in requester.
  Tenant and actor come from the session; the action must match one of
  the principal's exact-scope action grants (SAT21 otherwise). The
  insert is the normal admission bundle (operation, INTENT_RECORDED
  audit, no-dispatch outbox) with the provider key derived in SQL. The
  same request key again returns the same operation; a changed request
  under that key is a conflict (SAT22), never a silent reuse.
- safeact_api_create_workflow binds the fixed two-step workflow to its
  requester: grants for both the charge and the order are required,
  created_by is the principal, and the plan's payloads must be exactly
  the requested values.
- Reads (safeact_api_operations, safeact_api_operation,
  safeact_api_workflow) are tenant-scoped. A requester sees the
  operations they requested; an investigator or reviewer sees the
  tenant's. Anything else is reported as unknown (not found).
- Cancellation (deviation 0012): RECORDED -> CANCELLED_BEFORE_CLAIM, a
  new terminal state. Legal only as the first transition, with no
  attempt, under the cancellation's own id (never a worker claim); the
  append function enforces it. Claim, relay and fence already exclude
  any operation that has left RECORDED. Any other state records
  CANCELLATION_REQUEST_REFUSED and changes nothing: a request to cancel
  never implies that an in-flight remote action was undone. Workflow
  steps are never cancelled here, and safeact_workflow_status_of reads
  any cancelled step as INVALID.

Pinned by tests/phase_a/test_migration_sql_frozen.py. Values are
literals; nothing is read from core.
"""

from psycopg import sql

from storage.lifecycle_sql import v0003, v0008, v0009, v0011


REVISION = "0012_product_admission"

ACTION_NOT_GRANTED = "SAT21"
REQUEST_CONFLICT = "SAT22"
REQUEST_INVALID = "SAT23"

CANCELLED = "CANCELLED_BEFORE_CLAIM"
CANCEL_REASON = "cancelled_before_claim"
STATES = v0003.STATES + (CANCELLED,)
MAX_REQUEST_KEY = 255
MAX_PAYLOAD_BYTES = 16384
ORDER_POLICY = "refund_charge_when_order_failed_confirmed"

NEW_TABLES = ()
HISTORY_TABLES = ()
LEGAL_TRANSITIONS = frozenset({("RECORDED", CANCELLED)})

NEW_FUNCTIONS = {
    "safeact_api_admit": (
        "uuid, text, text, text, text, text, text, text, text"
    ),
    "safeact_api_requested_by": "uuid",
    "safeact_api_visible": "uuid, uuid, text, text[]",
    "safeact_api_submit": (
        "text, text, text, text, text, text, text, text, text"
    ),
    "safeact_api_create_workflow": (
        "text, text, text, text, text, text, bigint, text, text, "
        "text, text, text, text, text"
    ),
    "safeact_api_operations": (
        "text, text, text, boolean, timestamp with time zone, uuid, integer"
    ),
    "safeact_api_operation": "text, uuid",
    "safeact_api_workflow": "text, uuid",
    "safeact_api_cancel": "text, text, uuid",
    "safeact_api_my_grants": "text",
}
REPLACED_FUNCTIONS = (
    "safeact_lifecycle_append",
    "safeact_workflow_status_of",
)

SHARED_FUNCTIONS = {}
WORKER_FUNCTIONS = {}
RECOVERY_FUNCTIONS = {}
ADVANCER_FUNCTIONS = {}
INTERNAL_FUNCTIONS = {
    "safeact_api_admit": NEW_FUNCTIONS["safeact_api_admit"],
    "safeact_api_requested_by": "uuid",
    "safeact_api_visible": "uuid, uuid, text, text[]",
}
API_FUNCTIONS = {
    name: arguments for name, arguments in NEW_FUNCTIONS.items()
    if name not in INTERNAL_FUNCTIONS
}
# Full privilege sets (the head merge replaces per table).
OWNER_TABLE_PRIVILEGES = {
    "safeact_workflows": ("SELECT", "INSERT"),
    # Who requested an operation is read from its INTENT_RECORDED event.
    "safeact_audit_events": ("SELECT", "INSERT"),
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
        not_granted=sql.Literal(ACTION_NOT_GRANTED),
        conflict=sql.Literal(REQUEST_CONFLICT),
        invalid=sql.Literal(REQUEST_INVALID),
        role_required=sql.Literal(v0011.ROLE_REQUIRED),
        cancelled=sql.Literal(CANCELLED),
        cancel_reason=sql.Literal(CANCEL_REASON),
        max_key=sql.Literal(MAX_REQUEST_KEY),
        max_payload=sql.Literal(MAX_PAYLOAD_BYTES),
        max_page=sql.Literal(v0003.MAX_PAGE),
        policy=sql.Literal(ORDER_POLICY),
        workflow_type=sql.Literal(v0009.WORKFLOW_TYPE),
        view=sql.Identifier(v0003.CURRENT_STATE_VIEW),
        derived_key=sql.SQL(v0008.DERIVED_KEY.replace(
            "tenant_id", "p_tenant"
        ).replace(
            "request_key", "p_request_key"
        ).replace(
            "connector_id", "p_connector"
        ).replace(
            "provider_account_id", "p_account"
        )),
    )


def _states(s):
    table = sql.SQL("{}.{}").format(
        s, sql.Identifier("safeact_legal_transitions")
    )
    states = sql.SQL(", ").join(sql.Literal(v) for v in sorted(STATES))
    return [
        sql.SQL(
            "ALTER TABLE {} DROP CONSTRAINT ck_safeact_legal_transitions_states"
        ).format(table),
        sql.SQL(
            "ALTER TABLE {} ADD CONSTRAINT ck_safeact_legal_transitions_states "
            "CHECK (from_state IN ({states}) AND to_state IN ({states}))"
        ).format(table, states=states),
        sql.SQL(
            "INSERT INTO {} (from_state, to_state) VALUES ({}, {})"
        ).format(table, sql.Literal("RECORDED"), sql.Literal(CANCELLED)),
    ]


def _append(s):
    # The frozen 0008 append, with one guard added before its insert: a
    # cancellation is the first and only transition of an operation that
    # was never claimed or tried. Built from 0008's rendered text so
    # every earlier rule stays byte-for-byte the same.
    base = v0008._append(s).as_string(None)
    marker = (
        "    INSERT INTO " + s.as_string(None)
        + ".safeact_operation_transitions ("
    )
    if base.count(marker) != 1:
        raise RuntimeError("0008 append no longer has one transition insert")
    head, tail = base.split(marker)
    guard = sql.SQL("""    -- 0012: cancellation leaves before anything could be dispatched:
    -- the first transition, under the cancellation's own id, with no
    -- attempt and no claim ever recorded.
    IF p_to = {cancelled} AND (
        p_seq <> 1 OR p_from <> 'RECORDED'
        OR p_claim IS NULL OR p_attempt IS NOT NULL OR p_lease IS NOT NULL
        OR p_reason <> {cancel_reason}
        OR EXISTS (
            SELECT 1 FROM {s}.safeact_attempts a
            WHERE a.operation_id = p_operation
        )
        OR EXISTS (
            SELECT 1 FROM {s}.safeact_operation_transitions t
            WHERE t.operation_id = p_operation
        )
    ) THEN
        RAISE EXCEPTION 'Cancellation is only possible before any claim'
            USING ERRCODE = {illegal};
    END IF;

""").format(
        s=s, cancelled=sql.Literal(CANCELLED),
        cancel_reason=sql.Literal(CANCEL_REASON),
        illegal=sql.Literal(v0003.ILLEGAL_TRANSITION),
    ).as_string(None)
    return sql.SQL(head + guard + marker + tail)


def _status_of(s):
    # The frozen 0009 status, plus: a cancelled step is never part of a
    # valid workflow (the cancel function refuses workflow steps), so it
    # can never read as compensating or unresolved.
    base = v0009._status_of(s).as_string(None)
    if not base.startswith("CREATE FUNCTION") or base.count("BEGIN") != 1:
        raise RuntimeError("0009 status function changed shape")
    head, tail = base.split("BEGIN", 1)
    guard = sql.SQL("""BEGIN
    IF {cancelled} IN (p_charge, p_order, p_compensation) THEN
        RETURN 'INVALID';
    END IF;
""").format(cancelled=sql.Literal(CANCELLED)).as_string(None)
    text = head + guard + tail
    return sql.SQL("CREATE OR REPLACE" + text[len("CREATE"):])


def _admit(s):
    return _function(
        s, "safeact_api_admit",
        "p_principal uuid, p_tenant text, p_request_key text, "
        "p_action text, p_resource text, p_account text, "
        "p_connector text, p_version text, p_payload text",
        "TABLE (admitted_operation uuid, created boolean)", """
#variable_conflict use_column
DECLARE
    v_operation uuid;
    v_existing {s}.safeact_operations%ROWTYPE;
    v_requester text;
    v_key text;
    v_payload jsonb;
BEGIN
    IF p_request_key IS NULL
       OR length(p_request_key) NOT BETWEEN 1 AND {max_key}
       OR p_request_key ~ '[[:cntrl:][:space:]]'
       OR p_payload IS NULL
       OR octet_length(p_payload) > {max_payload} THEN
        RAISE EXCEPTION 'Invalid request' USING ERRCODE = {invalid};
    END IF;

    BEGIN
        v_payload := p_payload::jsonb;
    EXCEPTION WHEN others THEN
        RAISE EXCEPTION 'Invalid payload' USING ERRCODE = {invalid};
    END;
    IF jsonb_typeof(v_payload) <> 'object' THEN
        RAISE EXCEPTION 'Invalid payload' USING ERRCODE = {invalid};
    END IF;

    v_key := CASE WHEN EXISTS (
        SELECT 1 FROM {s}.safeact_connector_contracts k
        WHERE k.connector_id = p_connector
          AND k.version = p_version
          AND k.native_idempotency
    ) THEN {derived_key} END;

    INSERT INTO {s}.safeact_operations AS o (
        operation_id, tenant_id, request_key, action_type, resource_id,
        provider_account_id, connector_id, contract_version,
        payload_sha256, payload_canonical, provider_idempotency_key
    ) VALUES (
        gen_random_uuid(), p_tenant, p_request_key, p_action, p_resource,
        p_account, p_connector, p_version,
        encode(sha256(convert_to(p_payload, 'UTF8')), 'hex'),
        p_payload, v_key
    )
    ON CONFLICT ON CONSTRAINT uq_safeact_operations_tenant_request
    DO NOTHING
    RETURNING o.operation_id INTO v_operation;

    IF v_operation IS NOT NULL THEN
        INSERT INTO {s}.safeact_audit_events (
            operation_id, event_type, actor_id, details
        ) VALUES (
            v_operation, 'INTENT_RECORDED', p_principal::text,
            jsonb_build_object(
                'schema_version', 1,
                'dispatch_authorized', false,
                'channel', 'api'
            )
        );
        INSERT INTO {s}.safeact_outbox_events (
            outbox_id, operation_id, event_type, payload
        ) VALUES (
            gen_random_uuid(), v_operation, 'INTENT_RECORDED_NO_DISPATCH',
            jsonb_build_object('dispatch_enabled', false)
        );
        RETURN QUERY SELECT v_operation, true;
        RETURN;
    END IF;

    -- The same key again: the same operation, or a conflict. Never a
    -- silent reuse of a different request's outcome.
    SELECT * INTO v_existing FROM {s}.safeact_operations o
    WHERE o.tenant_id = p_tenant AND o.request_key = p_request_key;

    v_requester := {s}.safeact_api_requested_by(v_existing.operation_id);

    IF v_existing.action_type IS DISTINCT FROM p_action
       OR v_existing.resource_id IS DISTINCT FROM p_resource
       OR v_existing.provider_account_id IS DISTINCT FROM p_account
       OR v_existing.connector_id IS DISTINCT FROM p_connector
       OR v_existing.contract_version IS DISTINCT FROM p_version
       OR v_existing.payload_canonical IS DISTINCT FROM p_payload
       OR v_requester IS DISTINCT FROM p_principal::text THEN
        RAISE EXCEPTION 'Request key reused for a different request'
            USING ERRCODE = {conflict};
    END IF;

    RETURN QUERY SELECT v_existing.operation_id, false;
END;
""", definer=False)


def _requested_by(s):
    # The principal behind an operation: the workflow's creator for a
    # workflow step, otherwise the actor that admitted it.
    return _function(
        s, "safeact_api_requested_by", "p_operation uuid", "text", """
BEGIN
    RETURN coalesce(
        (SELECT w.created_by::text
           FROM {s}.safeact_workflow_steps st
           JOIN {s}.safeact_workflows w ON w.workflow_id = st.workflow_id
          WHERE st.operation_id = p_operation),
        (SELECT a.actor_id::text
           FROM {s}.safeact_audit_events a
          WHERE a.operation_id = p_operation
            AND a.event_type = 'INTENT_RECORDED'
          ORDER BY a.audit_id
          LIMIT 1)
    );
END;
""", definer=False, volatility="STABLE")


def _visible(s):
    return _function(
        s, "safeact_api_visible",
        "p_operation uuid, p_principal uuid, p_tenant text, p_roles text[]",
        "boolean", """
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM {s}.safeact_operations o
        WHERE o.operation_id = p_operation AND o.tenant_id = p_tenant
    ) AND (
        p_roles && ARRAY['investigator', 'reviewer']::text[]
        OR {s}.safeact_api_requested_by(p_operation) = p_principal::text
    );
END;
""", definer=False, volatility="STABLE")


def _submit(s):
    return _function(
        s, "safeact_api_submit",
        "p_secret text, p_csrf text, p_request_key text, p_action text, "
        "p_resource text, p_account text, p_connector text, "
        "p_version text, p_payload text",
        "TABLE (admitted_operation uuid, created boolean)", """
#variable_conflict use_column
DECLARE
    v record;
BEGIN
    SELECT * INTO v
      FROM {s}.safeact_api_session(p_secret, p_csrf, true, 'requester');

    -- Workflow steps use 'wf:' keys; a direct submission never does.
    IF p_request_key LIKE 'wf:%' THEN
        RAISE EXCEPTION 'Invalid request' USING ERRCODE = {invalid};
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM {s}.safeact_action_grants g
        WHERE g.principal_id = v.principal_id
          AND g.connector_id = p_connector
          AND g.provider_account_id = p_account
          AND g.action_type = p_action
          AND g.resource_id = p_resource
          AND g.contract_version = p_version
    ) THEN
        RAISE EXCEPTION 'Action not granted' USING ERRCODE = {not_granted};
    END IF;

    RETURN QUERY SELECT * FROM {s}.safeact_api_admit(
        v.principal_id, v.tenant_id, p_request_key, p_action, p_resource,
        p_account, p_connector, p_version, p_payload
    );
END;
""")


def _create_workflow(s):
    return _function(
        s, "safeact_api_create_workflow",
        "p_secret text, p_csrf text, p_request_key text, "
        "p_customer text, p_case text, p_sku text, p_fee bigint, "
        "p_payments_account text, p_crm_account text, "
        "p_charge_version text, p_order_version text, "
        "p_compensation_version text, p_charge_payload text, "
        "p_order_payload text",
        "TABLE (workflow uuid, charge_operation uuid, created boolean)", """
#variable_conflict use_column
DECLARE
    v record;
    v_plan jsonb;
    v_workflow uuid;
    v_existing record;
    v_charge uuid;
BEGIN
    SELECT * INTO v
      FROM {s}.safeact_api_session(p_secret, p_csrf, true, 'requester');

    IF p_request_key IS NULL
       OR length(p_request_key) NOT BETWEEN 1 AND {max_key}
       OR p_request_key ~ '[[:cntrl:][:space:]]'
       OR p_request_key LIKE 'wf:%'
       OR p_fee IS NULL OR p_fee <= 0
       OR p_charge_payload IS NULL OR p_order_payload IS NULL THEN
        RAISE EXCEPTION 'Invalid request' USING ERRCODE = {invalid};
    END IF;

    -- The payloads must be exactly the requested values.
    BEGIN
        IF p_charge_payload::jsonb IS DISTINCT FROM jsonb_build_object(
               'customer_id', p_customer, 'amount_minor', p_fee)
           OR p_order_payload::jsonb IS DISTINCT FROM jsonb_build_object(
               'case_id', p_case, 'sku', p_sku) THEN
            RAISE EXCEPTION 'Invalid payload' USING ERRCODE = {invalid};
        END IF;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION 'Invalid payload' USING ERRCODE = {invalid};
    END;

    -- The requester must hold both forward steps. The compensation is
    -- the policy recorded with the workflow, never a free action.
    IF NOT EXISTS (
        SELECT 1 FROM {s}.safeact_action_grants g
        WHERE g.principal_id = v.principal_id
          AND g.connector_id = 'mock-payments'
          AND g.provider_account_id = p_payments_account
          AND g.action_type = 'charge_shipping_fee'
          AND g.resource_id = p_customer
          AND g.contract_version = p_charge_version
    ) OR NOT EXISTS (
        SELECT 1 FROM {s}.safeact_action_grants g
        WHERE g.principal_id = v.principal_id
          AND g.connector_id = 'mock-crm'
          AND g.provider_account_id = p_crm_account
          AND g.action_type = 'create_replacement_order'
          AND g.resource_id = p_case
          AND g.contract_version = p_order_version
    ) THEN
        RAISE EXCEPTION 'Action not granted' USING ERRCODE = {not_granted};
    END IF;

    v_plan := jsonb_build_object(
        'order', jsonb_build_object(
            'action_type', 'create_replacement_order',
            'resource_id', p_case,
            'provider_account_id', p_crm_account,
            'connector_id', 'mock-crm',
            'contract_version', p_order_version,
            'payload_canonical', p_order_payload
        ),
        'compensation', jsonb_build_object(
            'policy', {policy},
            'action_type', 'refund_payment',
            'connector_id', 'mock-payments',
            'provider_account_id', p_payments_account,
            'contract_version', p_compensation_version
        )
    );

    INSERT INTO {s}.safeact_workflows AS w (
        workflow_id, tenant_id, request_key, workflow_type, created_by, plan
    ) VALUES (
        gen_random_uuid(), v.tenant_id, p_request_key, {workflow_type},
        v.principal_id::text, v_plan
    )
    ON CONFLICT ON CONSTRAINT uq_safeact_workflows_tenant_request
    DO NOTHING
    RETURNING w.workflow_id INTO v_workflow;

    IF v_workflow IS NULL THEN
        SELECT w.workflow_id, w.plan, w.created_by INTO v_existing
          FROM {s}.safeact_workflows w
         WHERE w.tenant_id = v.tenant_id AND w.request_key = p_request_key;
        IF v_existing.plan IS DISTINCT FROM v_plan
           OR v_existing.created_by IS DISTINCT FROM v.principal_id::text THEN
            RAISE EXCEPTION 'Request key reused for a different workflow'
                USING ERRCODE = {conflict};
        END IF;
        RETURN QUERY
        SELECT v_existing.workflow_id, st.operation_id, false
          FROM {s}.safeact_workflow_steps st
         WHERE st.workflow_id = v_existing.workflow_id
           AND st.step = 'charge';
        RETURN;
    END IF;

    SELECT a.admitted_operation INTO v_charge
      FROM {s}.safeact_api_admit(
          v.principal_id, v.tenant_id,
          'wf:' || v_workflow::text || ':charge',
          'charge_shipping_fee', p_customer, p_payments_account,
          'mock-payments', p_charge_version, p_charge_payload
      ) AS a;

    INSERT INTO {s}.safeact_workflow_steps (workflow_id, step, operation_id)
    VALUES (v_workflow, 'charge', v_charge);

    RETURN QUERY SELECT v_workflow, v_charge, true;
END;
""")


def _operations(s):
    return _function(
        s, "safeact_api_operations",
        "p_secret text, p_state text, p_connector text, "
        "p_unresolved_only boolean, p_before timestamptz, "
        "p_before_id uuid, p_limit integer",
        "TABLE (operation uuid, request_key text, action_type text, "
        "resource_id text, connector_id text, contract_version text, "
        "state text, seq integer, unknown_reason text, "
        "requested_by text, created_at timestamptz, "
        "updated_at timestamptz)", """
#variable_conflict use_column
DECLARE
    v record;
BEGIN
    SELECT * INTO v
      FROM {s}.safeact_api_session(p_secret, NULL, false, NULL);
    IF cardinality(v.roles) = 0 THEN
        RAISE EXCEPTION 'Role required' USING ERRCODE = {role_required};
    END IF;
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND {max_page} THEN
        RAISE EXCEPTION 'Invalid page size' USING ERRCODE = {invalid};
    END IF;

    RETURN QUERY
    SELECT o.operation_id, o.request_key::text, o.action_type::text,
           o.resource_id::text, o.connector_id::text,
           o.contract_version::text, c.state::text, c.seq,
           CASE WHEN c.state IN ('UNKNOWN', 'VERIFYING') THEN (
               SELECT t.reason::text
                 FROM {s}.safeact_operation_transitions t
                WHERE t.operation_id = o.operation_id
                  AND t.from_state = 'DISPATCHING'
                  AND t.to_state = 'UNKNOWN'
                ORDER BY t.seq DESC LIMIT 1
           ) END,
           {s}.safeact_api_requested_by(o.operation_id),
           o.created_at,
           coalesce(c.state_changed_at, o.created_at)
      FROM {s}.safeact_operations o
      JOIN {s}.{view} c ON c.operation_id = o.operation_id
     WHERE o.tenant_id = v.tenant_id
       AND (v.roles && ARRAY['investigator', 'reviewer']::text[]
            OR {s}.safeact_api_requested_by(o.operation_id)
               = v.principal_id::text)
       AND (p_state IS NULL OR c.state = p_state)
       AND (p_connector IS NULL OR o.connector_id = p_connector)
       AND (p_unresolved_only IS NOT TRUE OR c.state IN (
           'UNKNOWN', 'VERIFYING', 'ESCALATED_UNRESOLVED'))
       AND (p_before IS NULL
            OR (o.created_at, o.operation_id) < (p_before, p_before_id))
     ORDER BY o.created_at DESC, o.operation_id DESC
     LIMIT p_limit;
END;
""")


def _operation(s):
    return _function(
        s, "safeact_api_operation", "p_secret text, p_operation uuid",
        "jsonb", """
DECLARE
    v record;
    v_state record;
    v_result jsonb;
BEGIN
    SELECT * INTO v
      FROM {s}.safeact_api_session(p_secret, NULL, false, NULL);

    IF p_operation IS NULL OR NOT {s}.safeact_api_visible(
        p_operation, v.principal_id, v.tenant_id, v.roles
    ) THEN
        RAISE EXCEPTION 'Unknown operation' USING ERRCODE = {unknown};
    END IF;

    SELECT c.state, c.seq INTO v_state
      FROM {s}.{view} c WHERE c.operation_id = p_operation;

    SELECT jsonb_build_object(
        'operation_id', o.operation_id,
        'request_key', o.request_key,
        'action_type', o.action_type,
        'resource_id', o.resource_id,
        'provider_account_id', o.provider_account_id,
        'connector_id', o.connector_id,
        'contract_version', o.contract_version,
        'payload', o.payload_canonical::jsonb,
        'payload_sha256', o.payload_sha256,
        'provider_key_protected', o.provider_idempotency_key IS NOT NULL,
        'created_at', o.created_at,
        'requested_by', {s}.safeact_api_requested_by(o.operation_id),
        'state', v_state.state,
        'seq', v_state.seq,
        'outcome_class', CASE
            WHEN v_state.state IN ('SUCCEEDED', 'FAILED_CONFIRMED') THEN 'confirmed'
            WHEN v_state.state = {cancelled} THEN 'cancelled'
            WHEN v_state.state IN ('UNKNOWN', 'VERIFYING', 'ESCALATED_UNRESOLVED') THEN 'unknown'
            ELSE 'in_progress' END,
        'workflow', (
            SELECT jsonb_build_object('workflow_id', st.workflow_id, 'step', st.step)
              FROM {s}.safeact_workflow_steps st
             WHERE st.operation_id = o.operation_id
        ),
        'contract', (
            SELECT jsonb_build_object(
                'connector_id', k.connector_id,
                'version', k.version,
                'side_effect_class', k.side_effect_class,
                'native_idempotency', k.native_idempotency,
                'key_retention_seconds', k.key_retention_seconds,
                'key_binds_parameters', k.key_binds_parameters,
                'key_protects_concurrent', k.key_protects_concurrent,
                'lookup_semantics', k.lookup_semantics,
                'request_deadline_seconds', k.request_deadline_seconds,
                'investigation_window_seconds', k.investigation_window_seconds,
                'max_attempts', k.max_attempts,
                'max_reopens', k.max_reopens,
                'compensation_action', k.compensation_action,
                'contract_reference', k.contract_reference
            )
              FROM {s}.safeact_connector_contracts k
             WHERE k.connector_id = o.connector_id
               AND k.version = o.contract_version
        ),
        'transitions', coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                'seq', t.seq,
                'from_state', t.from_state,
                'to_state', t.to_state,
                'reason', t.reason,
                'actor', t.actor,
                'attempt_id', t.attempt_id,
                'evidence', t.evidence,
                'created_at', t.created_at
            ) ORDER BY t.seq)
              FROM {s}.safeact_operation_transitions t
             WHERE t.operation_id = o.operation_id
        ), '[]'::jsonb),
        'attempts', coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                'attempt_id', a.attempt_id,
                'ordinal', a.ordinal,
                'created_at', a.created_at,
                'outcomes', coalesce((
                    SELECT jsonb_agg(jsonb_build_object(
                        'outcome', x.outcome,
                        'evidence_kind', x.evidence_kind,
                        'evidence_reference', x.evidence_reference,
                        'observed_by', x.observed_by,
                        'observed_seq', x.observed_seq,
                        'details', x.details,
                        'created_at', x.created_at
                    ) ORDER BY x.created_at, x.outcome_id)
                      FROM {s}.safeact_attempt_outcomes x
                     WHERE x.attempt_id = a.attempt_id
                ), '[]'::jsonb)
            ) ORDER BY a.ordinal)
              FROM {s}.safeact_attempts a
             WHERE a.operation_id = o.operation_id
        ), '[]'::jsonb),
        'cancellation_requests', coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                'requested_by', e.actor_id,
                'details', e.details,
                'created_at', e.created_at
            ) ORDER BY e.audit_id)
              FROM {s}.safeact_audit_events e
             WHERE e.operation_id = o.operation_id
               AND e.event_type IN (
                   'CANCELLATION_REQUEST_REFUSED', 'CANCELLATION_REQUEST_ACCEPTED'
               )
        ), '[]'::jsonb)
    ) INTO v_result
      FROM {s}.safeact_operations o
     WHERE o.operation_id = p_operation;

    RETURN v_result;
END;
""", volatility="VOLATILE")


def _workflow(s):
    return _function(
        s, "safeact_api_workflow", "p_secret text, p_workflow uuid",
        "jsonb", """
DECLARE
    v record;
    v_result jsonb;
BEGIN
    SELECT * INTO v
      FROM {s}.safeact_api_session(p_secret, NULL, false, NULL);

    SELECT jsonb_build_object(
        'workflow_id', w.workflow_id,
        'request_key', w.request_key,
        'workflow_type', w.workflow_type,
        'created_by', w.created_by,
        'created_at', w.created_at,
        'plan', w.plan,
        -- The status the workflow view derives, from the same function.
        'status', {s}.safeact_workflow_status_of(
            (SELECT c.state FROM {s}.safeact_workflow_steps st
               JOIN {s}.{view} c ON c.operation_id = st.operation_id
              WHERE st.workflow_id = w.workflow_id AND st.step = 'charge'),
            (SELECT c.state FROM {s}.safeact_workflow_steps st
               JOIN {s}.{view} c ON c.operation_id = st.operation_id
              WHERE st.workflow_id = w.workflow_id AND st.step = 'order'),
            (SELECT c.state FROM {s}.safeact_workflow_steps st
               JOIN {s}.{view} c ON c.operation_id = st.operation_id
              WHERE st.workflow_id = w.workflow_id
                AND st.step = 'compensation')
        ),
        'steps', coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                'step', st.step,
                'operation_id', st.operation_id,
                'state', c.state,
                'compensates_operation_id', st.compensates_operation_id
            ) ORDER BY st.created_at, st.step)
              FROM {s}.safeact_workflow_steps st
              JOIN {s}.{view} c ON c.operation_id = st.operation_id
             WHERE st.workflow_id = w.workflow_id
        ), '[]'::jsonb)
    ) INTO v_result
      FROM {s}.safeact_workflows w
     WHERE w.workflow_id = p_workflow
       AND w.tenant_id = v.tenant_id
       AND (v.roles && ARRAY['investigator', 'reviewer']::text[]
            OR w.created_by = v.principal_id::text);

    IF v_result IS NULL THEN
        RAISE EXCEPTION 'Unknown workflow' USING ERRCODE = {unknown};
    END IF;
    RETURN v_result;
END;
""")


def _cancel(s):
    return _function(
        s, "safeact_api_cancel",
        "p_secret text, p_csrf text, p_operation uuid",
        "TABLE (outcome text, state text, seq integer)", """
#variable_conflict use_column
DECLARE
    v record;
    v_lock record;
    v_refusal text;
BEGIN
    SELECT * INTO v
      FROM {s}.safeact_api_session(p_secret, p_csrf, true, NULL);

    IF p_operation IS NULL OR NOT {s}.safeact_api_visible(
        p_operation, v.principal_id, v.tenant_id, v.roles
    ) THEN
        RAISE EXCEPTION 'Unknown operation' USING ERRCODE = {unknown};
    END IF;

    -- The operation's own requester, or an investigator, may ask.
    IF NOT ('investigator' = ANY (v.roles) OR (
        'requester' = ANY (v.roles)
        AND {s}.safeact_api_requested_by(p_operation) = v.principal_id::text
    )) THEN
        RAISE EXCEPTION 'Role required' USING ERRCODE = {role_required};
    END IF;

    SELECT * INTO v_lock FROM {s}.safeact_lifecycle_lock(p_operation);

    v_refusal := CASE
        WHEN EXISTS (
            SELECT 1 FROM {s}.safeact_workflow_steps st
            WHERE st.operation_id = p_operation
        ) THEN 'workflow_step'
        WHEN v_lock.o_seq = 0 AND v_lock.o_state = 'RECORDED'
             AND NOT EXISTS (
                 SELECT 1 FROM {s}.safeact_attempts a
                 WHERE a.operation_id = p_operation
             ) THEN NULL
        WHEN v_lock.o_state IN (
            'SUCCEEDED', 'FAILED_CONFIRMED', {cancelled}
        ) THEN 'already_final'
        ELSE 'may_have_dispatched'
    END;

    IF v_refusal IS NULL THEN
        PERFORM {s}.safeact_lifecycle_append(
            p_operation, 1, 'RECORDED', {cancelled}, gen_random_uuid(),
            NULL, NULL, {cancel_reason},
            jsonb_build_object(
                'requested_by', v.principal_id,
                'username', v.username
            ),
            'LIFECYCLE_CANCELLED'
        );
        INSERT INTO {s}.safeact_audit_events (
            operation_id, event_type, actor_id, details
        ) VALUES (
            p_operation, 'CANCELLATION_REQUEST_ACCEPTED',
            v.principal_id::text,
            jsonb_build_object('schema_version', 1, 'state', {cancelled})
        );
        RETURN QUERY SELECT 'cancelled'::text, {cancelled}::text, 1;
        RETURN;
    END IF;

    -- Refused: recorded, nothing changed. A refusal never claims that
    -- an in-flight remote action was stopped or undone.
    INSERT INTO {s}.safeact_audit_events (
        operation_id, event_type, actor_id, details
    ) VALUES (
        p_operation, 'CANCELLATION_REQUEST_REFUSED', v.principal_id::text,
        jsonb_build_object(
            'schema_version', 1,
            'reason', v_refusal,
            'state', v_lock.o_state,
            'seq', v_lock.o_seq
        )
    );
    RETURN QUERY SELECT 'refused_' || v_refusal, v_lock.o_state::text,
                        v_lock.o_seq;
END;
""")


def _my_grants(s):
    return _function(
        s, "safeact_api_my_grants", "p_secret text",
        "TABLE (connector_id text, provider_account_id text, "
        "action_type text, resource_id text, contract_version text)", """
#variable_conflict use_column
DECLARE
    v record;
BEGIN
    SELECT * INTO v
      FROM {s}.safeact_api_session(p_secret, NULL, false, NULL);

    -- Only the signed-in principal's own grants.
    RETURN QUERY
    SELECT g.connector_id::text, g.provider_account_id::text,
           g.action_type::text, g.resource_id::text,
           g.contract_version::text
      FROM {s}.safeact_action_grants g
     WHERE g.principal_id = v.principal_id
     ORDER BY g.action_type, g.resource_id, g.provider_account_id,
              g.contract_version;
END;
""", volatility="VOLATILE")


def _signature(s, name, arguments):
    return sql.SQL("{}.{}({})").format(
        s, sql.Identifier(name), sql.SQL(arguments)
    )


_BUILDERS = (
    _requested_by, _visible, _admit, _submit, _create_workflow,
    _operations, _operation, _workflow, _cancel, _my_grants,
)


def upgrade_statements(schema):
    s = v0003._schema(schema)
    return _states(s) + [_append(s), _status_of(s)] + [
        build(s) for build in _BUILDERS
    ] + [
        sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(
            _signature(s, name, arguments)
        )
        for name, arguments in NEW_FUNCTIONS.items()
    ]


def downgrade_statements(schema):
    s = v0003._schema(schema)
    table = sql.SQL("{}.{}").format(
        s, sql.Identifier("safeact_legal_transitions")
    )
    guard = sql.Identifier("safeact_legal_transitions_write_guard")
    states = sql.SQL(", ").join(
        sql.Literal(v) for v in sorted(v0003.STATES)
    )
    original = v0009._status_of(s).as_string(None)
    return [
        v0008._append(s),
        sql.SQL("CREATE OR REPLACE" + original[len("CREATE"):]),
    ] + [
        sql.SQL("DROP FUNCTION IF EXISTS {}").format(
            _signature(s, name, arguments)
        )
        for name, arguments in reversed(tuple(NEW_FUNCTIONS.items()))
    ] + [
        sql.SQL("ALTER TABLE {} DISABLE TRIGGER {}").format(table, guard),
        sql.SQL(
            "DELETE FROM {} WHERE from_state = {} AND to_state = {}"
        ).format(table, sql.Literal("RECORDED"), sql.Literal(CANCELLED)),
        sql.SQL("ALTER TABLE {} ENABLE TRIGGER {}").format(table, guard),
        sql.SQL(
            "ALTER TABLE {} DROP CONSTRAINT ck_safeact_legal_transitions_states"
        ).format(table),
        sql.SQL(
            "ALTER TABLE {} ADD CONSTRAINT ck_safeact_legal_transitions_states "
            "CHECK (from_state IN ({states}) AND to_state IN ({states}))"
        ).format(table, states=states),
    ]
