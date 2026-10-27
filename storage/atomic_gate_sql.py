"""Disposable SQL prototype for SafeAct atomic authority admission.

For isolated-schema tests only. It accepts asserted issuer/subject and
must not be treated as a production identity verification boundary.
"""

import re
from psycopg import sql


def _identifier(schema: str):
    if type(schema) is not str or re.fullmatch(
        r"safeact_it_[0-9a-f]{32}", schema
    ) is None:
        raise ValueError(
            "Only isolated disposable test schemas are allowed"
        )
    return sql.Identifier(schema)


def create_gate_sql(schema: str):
    s = _identifier(schema)
    return sql.SQL(r"""
CREATE FUNCTION {s}.admit_real_intent(
    p_issuer text, p_subject text, p_key text,
    p_action text, p_resource text, p_connector text,
    p_account text, p_contract text, p_payload text,
    p_digest text, p_provider_key text
)
RETURNS TABLE(operation_id uuid, created boolean)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog
AS $safeact_gate$
DECLARE
    v_tenant text;
    v_actor text;
    v_new uuid;
    v_old record;
    v_audit_count bigint;
    v_audit_actor text;
    v_audit_ok boolean;
    v_outbox_count bigint;
    v_outbox_ok boolean;
BEGIN
    IF p_key IS NULL OR length(p_key) = 0
       OR length(p_key) > 255
       OR p_payload IS NULL OR p_digest IS NULL THEN
        RAISE EXCEPTION 'Invalid admission request'
            USING ERRCODE='42501';
    END IF;

    SELECT a.tenant_id, a.actor_id INTO v_tenant, v_actor
    FROM {s}.safeact_auth_identities i
    JOIN {s}.safeact_auth_actors a
      ON a.tenant_id=i.tenant_id AND a.actor_id=i.actor_id
    JOIN {s}.safeact_auth_resources r
      ON r.tenant_id=a.tenant_id
     AND r.resource_id=p_resource
     AND r.connector_id=p_connector
     AND r.provider_account_id=p_account
    JOIN {s}.safeact_auth_grants g
      ON g.tenant_id=a.tenant_id AND g.actor_id=a.actor_id
     AND g.resource_id=r.resource_id
     AND g.connector_id=r.connector_id
     AND g.provider_account_id=r.provider_account_id
     AND g.action_type=p_action AND g.contract_version=p_contract
    WHERE i.issuer=p_issuer AND i.subject=p_subject
      AND i.enabled AND a.enabled AND r.enabled AND g.enabled
    FOR SHARE OF i,a,r,g;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Admission denied'
            USING ERRCODE='42501';
    END IF;

    INSERT INTO {s}.safeact_operations AS o (
        operation_id, tenant_id, request_key, action_type,
        resource_id, provider_account_id, connector_id,
        contract_version, payload_sha256, payload_canonical,
        provider_idempotency_key
    ) VALUES (
        pg_catalog.gen_random_uuid(),v_tenant,p_key,p_action,
        p_resource,p_account,p_connector,p_contract,p_digest,
        p_payload,p_provider_key
    ) ON CONFLICT (tenant_id, request_key) DO NOTHING
    RETURNING o.operation_id INTO v_new;

    IF v_new IS NOT NULL THEN
        INSERT INTO {s}.safeact_audit_events (
            operation_id,event_type,actor_id,details
        ) VALUES (
            v_new,'INTENT_RECORDED',v_actor,
            pg_catalog.jsonb_build_object(
                'schema_version',1,'dispatch_authorized',FALSE
            )
        );
        INSERT INTO {s}.safeact_outbox_events (
            outbox_id,operation_id,event_type,payload
        ) VALUES (
            pg_catalog.gen_random_uuid(),v_new,
            'INTENT_RECORDED_NO_DISPATCH',
            pg_catalog.jsonb_build_object(
                'dispatch_enabled',FALSE
            )
        );
        RETURN QUERY SELECT v_new,TRUE;
        RETURN;
    END IF;

    SELECT o.* INTO v_old FROM {s}.safeact_operations o
    WHERE o.tenant_id=v_tenant AND o.request_key=p_key;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Conflicting operation not visible';
    END IF;

    IF v_old.action_type IS DISTINCT FROM p_action
       OR v_old.resource_id IS DISTINCT FROM p_resource
       OR v_old.provider_account_id IS DISTINCT FROM p_account
       OR v_old.connector_id IS DISTINCT FROM p_connector
       OR v_old.contract_version IS DISTINCT FROM p_contract
       OR v_old.payload_sha256 IS DISTINCT FROM p_digest
       OR v_old.payload_canonical IS DISTINCT FROM p_payload
       OR v_old.provider_idempotency_key IS DISTINCT FROM p_provider_key
       OR v_old.state IS DISTINCT FROM 'RECORDED'
       OR v_old.version IS DISTINCT FROM 0 THEN
        RAISE EXCEPTION 'Conflicting or uncertain operation';
    END IF;

    SELECT count(*), max(a.actor_id),
           bool_and(
               pg_catalog.jsonb_typeof(
                   a.details->'schema_version'
               )='number'
               AND a.details->>'schema_version'='1'
               AND a.details->'dispatch_authorized'='false'::jsonb
           )
    INTO v_audit_count,v_audit_actor,v_audit_ok
    FROM {s}.safeact_audit_events a
    WHERE a.operation_id=v_old.operation_id
      AND a.event_type='INTENT_RECORDED';

    IF v_audit_count<>1 OR v_audit_actor IS DISTINCT FROM v_actor
       OR v_audit_ok IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'Original actor/audit evidence denied'
            USING ERRCODE='42501';
    END IF;

    SELECT count(*), bool_and(
        o.state='PENDING'
        AND o.payload->'dispatch_enabled'='false'::jsonb
    ) INTO v_outbox_count,v_outbox_ok
    FROM {s}.safeact_outbox_events o
    WHERE o.operation_id=v_old.operation_id
      AND o.event_type='INTENT_RECORDED_NO_DISPATCH';

    IF v_outbox_count<>1 OR v_outbox_ok IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'Outbox evidence uncertain';
    END IF;

    RETURN QUERY SELECT v_old.operation_id,FALSE;
END;
$safeact_gate$
""").format(s=s)


def gate_signature_sql(schema: str):
    return sql.SQL(
        "{}.admit_real_intent("
        "text,text,text,text,text,text,"
        "text,text,text,text,text)"
    ).format(_identifier(schema))


def gate_call_sql(schema: str):
    return sql.SQL("""
        SELECT operation_id, created
        FROM {}.admit_real_intent(
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
        )
    """).format(_identifier(schema))
