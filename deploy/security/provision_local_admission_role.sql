-- SafeAct local-development permission role provisioning.
-- Execute inside a caller-controlled transaction.
-- No login, password, or permanent runtime credential is created.

DO $preflight$
DECLARE
    existing RECORD;
BEGIN
    IF current_database() <> 'safeact_dev'
       OR current_user <> 'safeact_dev'
    THEN
        RAISE EXCEPTION 'Unexpected administrator or database';
    END IF;

    IF (
        SELECT version_num
        FROM public.alembic_version
    ) IS DISTINCT FROM '0022_admission_race'
    THEN
        RAISE EXCEPTION 'Unexpected Alembic revision';
    END IF;

    SELECT
        rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
        rolreplication, rolbypassrls, rolinherit
    INTO existing
    FROM pg_roles
    WHERE rolname = 'safeact_admission_writer';

    IF FOUND THEN
        IF existing.rolcanlogin OR existing.rolsuper
           OR existing.rolcreatedb OR existing.rolcreaterole
           OR existing.rolreplication OR existing.rolbypassrls
           OR existing.rolinherit
        THEN
            RAISE EXCEPTION 'Existing writer role is privileged';
        END IF;
    ELSE
        EXECUTE '
            CREATE ROLE safeact_admission_writer
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOREPLICATION NOBYPASSRLS NOINHERIT
        ';
    END IF;
END
$preflight$;

GRANT USAGE ON SCHEMA public TO safeact_admission_writer;

GRANT SELECT, INSERT ON TABLE
    public.safeact_operations,
    public.safeact_audit_events,
    public.safeact_outbox_events
TO safeact_admission_writer;

DO $verification$
DECLARE
    relation_name text;
    privilege_name text;
    expected boolean;
    actual boolean;
BEGIN
    FOREACH relation_name IN ARRAY ARRAY[
        'safeact_operations', 'safeact_audit_events',
        'safeact_outbox_events', 'safeact_attempts'
    ] LOOP
        FOREACH privilege_name IN ARRAY ARRAY[
            'SELECT', 'INSERT', 'UPDATE', 'DELETE',
            'TRUNCATE', 'REFERENCES', 'TRIGGER'
        ] LOOP
            expected := (
                relation_name <> 'safeact_attempts'
                AND privilege_name IN ('SELECT', 'INSERT')
            );

            actual := has_table_privilege(
                'safeact_admission_writer',
                'public.' || relation_name,
                privilege_name
            );

            IF actual IS DISTINCT FROM expected THEN
                RAISE EXCEPTION
                    'Unexpected privilege %.%: %',
                    relation_name, privilege_name, actual;
            END IF;
        END LOOP;
    END LOOP;

    IF NOT has_schema_privilege(
        'safeact_admission_writer', 'public', 'USAGE'
    ) OR has_schema_privilege(
        'safeact_admission_writer', 'public', 'CREATE'
    ) OR has_database_privilege(
        'safeact_admission_writer', current_database(), 'CREATE'
    ) THEN
        RAISE EXCEPTION 'Unexpected schema/database grants';
    END IF;

    -- Lifecycle objects (revision 0003 on): admission gets nothing.
    FOREACH relation_name IN ARRAY ARRAY[
        'safeact_outbox_deliveries', 'safeact_attempt_outcomes', 'safeact_operation_transitions', 'safeact_legal_transitions', 'safeact_operation_current_state',
        'safeact_connector_contracts',
        'safeact_workflows', 'safeact_workflow_steps', 'safeact_workflow_status',
        'safeact_principals', 'safeact_principal_disablements', 'safeact_credentials',
        'safeact_role_grants', 'safeact_action_grants', 'safeact_login_attempts',
        'safeact_sessions', 'safeact_session_activity', 'safeact_session_revocations',
        'safeact_contract_review_policies', 'safeact_proposals',
        'safeact_decisions', 'safeact_reverify_uses',
        'safeact_verification_requests', 'safeact_verification_request_uses',
        'safeact_agent_keys', 'safeact_agent_key_revocations',
        'safeact_agent_call_sessions',
        'safeact_state_projection',
        'safeact_contract_limits', 'safeact_retry_schedule',
        'safeact_connector_health', 'safeact_connector_health_events'
    ] LOOP
        FOREACH privilege_name IN ARRAY ARRAY[
            'SELECT', 'INSERT', 'UPDATE', 'DELETE',
            'TRUNCATE', 'REFERENCES', 'TRIGGER'
        ] LOOP
            IF has_table_privilege(
                'safeact_admission_writer',
                'public.' || relation_name,
                privilege_name
            ) THEN
                RAISE EXCEPTION
                    'Unexpected lifecycle privilege %.%',
                    relation_name, privilege_name;
            END IF;
        END LOOP;
    END LOOP;

    FOREACH relation_name IN ARRAY ARRAY[
        'public.safeact_claim(uuid, integer)',
        'public.safeact_begin_dispatch(uuid, integer, uuid, integer)',
        'public.safeact_record_dispatch_outcome(uuid, integer, uuid, text, text, jsonb)',
        'public.safeact_record_outbox_delivery(uuid)',
        'public.safeact_claimable_operations(integer)',
        'public.safeact_relay_pending(integer)',
        'public.safeact_recovery_transition(uuid, integer, text, text, text, jsonb)',
        'public.safeact_operations_in_state(text, integer)',
        'public.safeact_recovery_snapshot(uuid)',
        'public.safeact_record_attempt_outcome(uuid, text, text, text, jsonb)',
        'public.safeact_lifecycle_lock(uuid)',
        'public.safeact_lifecycle_append(uuid, integer, text, text, uuid, uuid, timestamp with time zone, text, jsonb, text)',
        'public.safeact_connector_contract(text, text)',
        'public.safeact_investigation_deadline(uuid)',
        'public.safeact_investigation_status(uuid)',
        'public.safeact_release_claim(uuid, integer, uuid, text)',
        'public.safeact_retry_basis(uuid)',
        'public.safeact_attempt_budget(uuid)',
        'public.safeact_accounting()',
        'public.safeact_reclaim_blocked(uuid)',
        'public.safeact_reopen_evidence(uuid)',
        'public.safeact_workflow_status_of(text, text, text)',
        'public.safeact_admit_workflow_step(uuid, text)',
        'public.safeact_workflows_to_advance(integer)',
        'public.safeact_admit_compensation(uuid)',
        'public.safeact_identity_guard()',
        'public.safeact_password_matches(text, text)',
        'public.safeact_api_session(text, text, boolean, text)',
        'public.safeact_api_login(text, text, text)',
        'public.safeact_api_logout(text, text)',
        'public.safeact_api_whoami(text)',
        'public.safeact_api_admit(uuid, text, text, text, text, text, text, text, text)',
        'public.safeact_api_requested_by(uuid)',
        'public.safeact_api_visible(uuid, uuid, text, text[])',
        'public.safeact_api_submit(text, text, text, text, text, text, text, text, text)',
        'public.safeact_api_create_workflow(text, text, text, text, text, text, bigint, text, text, text, text, text, text, text)',
        'public.safeact_api_operations(text, text, text, boolean, timestamp with time zone, uuid, integer)',
        'public.safeact_api_operation(text, uuid)',
        'public.safeact_api_workflow(text, uuid)',
        'public.safeact_api_cancel(text, text, uuid)',
        'public.safeact_api_my_grants(text)',
        'public.safeact_evidence_digest(uuid)',
        'public.safeact_retry_approved(uuid, integer)',
        'public.safeact_proposal_eligibility(uuid, text, text)',
        'public.safeact_proposal_digest(uuid, text, text, timestamp with time zone)',
        'public.safeact_proposal_view(uuid)',
        'public.safeact_api_propose(text, text, uuid, text, text, text)',
        'public.safeact_api_proposals(text, text, integer)',
        'public.safeact_api_proposal(text, uuid)',
        'public.safeact_api_decide(text, text, uuid, text, text, uuid, text)',
        'public.safeact_retry_approval_status(uuid)',
        'public.safeact_take_reverify(uuid)',
        'public.safeact_succeeded_by_provenance()',
        'public.safeact_api_changes(text, text)',
        'public.safeact_verification_bounds(uuid)',
        'public.safeact_api_request_verification(text, text, uuid)',
        'public.safeact_take_verification_request(uuid)',
        'public.safeact_outcome_reasons()',
        'public.safeact_agent_key_guard()',
        'public.safeact_agent_call(text, text, text)',
        'public.safeact_mcp_submit(text, text, text, text, text, text, text, text)',
        'public.safeact_mcp_operation(text, uuid)',
        'public.safeact_mcp_request_verification(text, uuid)',
        'public.safeact_mcp_propose(text, uuid, text, text)',
        'public.safeact_state_projection_seed()',
        'public.safeact_state_projection_guard()',
        'public.safeact_backoff_seconds(uuid, integer)',
        'public.safeact_next_lookup_at(uuid)',
        'public.safeact_dispatch_gate(uuid, boolean)',
        'public.safeact_pacing(uuid)',
        'public.safeact_next_check(uuid)',
        'public.safeact_metrics()'
    ] LOOP
        IF has_function_privilege(
            'safeact_admission_writer', relation_name, 'EXECUTE'
        ) THEN
            RAISE EXCEPTION
                'Unexpected lifecycle function privilege %',
                relation_name;
        END IF;
    END LOOP;

    FOREACH privilege_name IN ARRAY ARRAY[
        'USAGE', 'SELECT', 'UPDATE'
    ] LOOP
        IF has_sequence_privilege(
            'safeact_admission_writer',
            'public.safeact_audit_events_audit_id_seq',
            privilege_name
        ) THEN
            RAISE EXCEPTION
                'Unexpected audit-sequence privilege %',
                privilege_name;
        END IF;
    END LOOP;
END
$verification$;
