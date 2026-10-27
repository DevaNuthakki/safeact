
CREATE TABLE "public".safeact_legal_transitions (
    from_state varchar(32) NOT NULL,
    to_state varchar(32) NOT NULL,
    PRIMARY KEY (from_state, to_state),
    CONSTRAINT ck_safeact_legal_transitions_states
        CHECK (from_state IN ('CLAIMED', 'DISPATCHING', 'ESCALATED_UNRESOLVED', 'FAILED_CONFIRMED', 'RECORDED', 'RETRY_ELIGIBLE', 'SUCCEEDED', 'UNKNOWN', 'VERIFYING')
           AND to_state IN ('CLAIMED', 'DISPATCHING', 'ESCALATED_UNRESOLVED', 'FAILED_CONFIRMED', 'RECORDED', 'RETRY_ELIGIBLE', 'SUCCEEDED', 'UNKNOWN', 'VERIFYING'))
)
-- safeact:statement --

INSERT INTO "public".safeact_legal_transitions (from_state, to_state)
VALUES ('CLAIMED', 'DISPATCHING'), ('DISPATCHING', 'FAILED_CONFIRMED'), ('DISPATCHING', 'SUCCEEDED'), ('DISPATCHING', 'UNKNOWN'), ('RECORDED', 'CLAIMED'), ('RETRY_ELIGIBLE', 'CLAIMED'), ('UNKNOWN', 'VERIFYING'), ('VERIFYING', 'ESCALATED_UNRESOLVED'), ('VERIFYING', 'FAILED_CONFIRMED'), ('VERIFYING', 'RETRY_ELIGIBLE'), ('VERIFYING', 'SUCCEEDED'), ('VERIFYING', 'UNKNOWN')
-- safeact:statement --

CREATE TABLE "public".safeact_operation_transitions (
    operation_id uuid NOT NULL
        REFERENCES "public".safeact_operations (operation_id)
        ON DELETE RESTRICT,
    seq integer NOT NULL,
    from_state varchar(32) NOT NULL,
    to_state varchar(32) NOT NULL,
    claim_id uuid NOT NULL,
    attempt_id uuid
        REFERENCES "public".safeact_attempts (attempt_id)
        ON DELETE RESTRICT,
    actor varchar(128) NOT NULL,
    lease_expires_at timestamptz,
    reason varchar(255) NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_safeact_operation_transitions
        PRIMARY KEY (operation_id, seq),
    CONSTRAINT fk_safeact_operation_transitions_legal
        FOREIGN KEY (from_state, to_state)
        REFERENCES "public".safeact_legal_transitions
            (from_state, to_state),
    CONSTRAINT ck_safeact_operation_transitions_seq
        CHECK (seq > 0 AND (seq = 1) = (from_state = 'RECORDED')),
    CONSTRAINT ck_safeact_operation_transitions_text
        CHECK (actor <> '' AND reason <> ''),
    CONSTRAINT ck_safeact_operation_transitions_evidence
        CHECK (jsonb_typeof(evidence) = 'object'),
    CONSTRAINT ck_safeact_operation_transitions_fence
        CHECK (to_state <> 'DISPATCHING' OR attempt_id IS NOT NULL),
    CONSTRAINT ck_safeact_operation_transitions_lease
        CHECK (to_state NOT IN ('CLAIMED', 'DISPATCHING')
               OR lease_expires_at IS NOT NULL)
)
-- safeact:statement --

CREATE INDEX ix_safeact_operation_transitions_state
ON "public".safeact_operation_transitions (to_state, operation_id, seq)

-- safeact:statement --

CREATE INDEX ix_safeact_operations_created_at
ON "public".safeact_operations (created_at, operation_id)

-- safeact:statement --

CREATE TABLE "public".safeact_attempt_outcomes (
    outcome_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL
        REFERENCES "public".safeact_operations (operation_id)
        ON DELETE RESTRICT,
    attempt_id uuid NOT NULL
        REFERENCES "public".safeact_attempts (attempt_id)
        ON DELETE RESTRICT,
    outcome varchar(32) NOT NULL,
    evidence_kind varchar(32),
    evidence_reference varchar(255),
    observed_by varchar(128) NOT NULL,
    observed_seq integer NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_safeact_attempt_outcomes_outcome
        CHECK (outcome IN ('confirmed_applied', 'may_have_dispatched', 'never_dispatched', 'proven_not_applied')),
    CONSTRAINT ck_safeact_attempt_outcomes_evidence
        CHECK (evidence_kind IS NULL
               OR evidence_kind IN ('applied', 'inconclusive', 'not_found', 'proven_not_applied')),
    CONSTRAINT ck_safeact_attempt_outcomes_proof
        CHECK (outcome NOT IN ('proven_not_applied',
                               'confirmed_applied')
               OR (evidence_reference IS NOT NULL
                   AND evidence_reference <> '')),
    CONSTRAINT ck_safeact_attempt_outcomes_details
        CHECK (jsonb_typeof(details) = 'object'),
    CONSTRAINT ck_safeact_attempt_outcomes_seq
        CHECK (observed_seq >= 0)
)
-- safeact:statement --

CREATE TABLE "public".safeact_outbox_deliveries (
    delivery_id uuid PRIMARY KEY,
    outbox_id uuid NOT NULL
        REFERENCES "public".safeact_outbox_events (outbox_id)
        ON DELETE RESTRICT,
    operation_id uuid NOT NULL
        REFERENCES "public".safeact_operations (operation_id)
        ON DELETE RESTRICT,
    published_by varchar(128) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
)
-- safeact:statement --
CREATE TRIGGER "safeact_outbox_deliveries_write_guard" BEFORE UPDATE OR DELETE ON "public"."safeact_outbox_deliveries" FOR EACH ROW EXECUTE FUNCTION public.safeact_reject_mutation()
-- safeact:statement --
CREATE TRIGGER "safeact_attempt_outcomes_write_guard" BEFORE UPDATE OR DELETE ON "public"."safeact_attempt_outcomes" FOR EACH ROW EXECUTE FUNCTION public.safeact_reject_mutation()
-- safeact:statement --
CREATE TRIGGER "safeact_operation_transitions_write_guard" BEFORE UPDATE OR DELETE ON "public"."safeact_operation_transitions" FOR EACH ROW EXECUTE FUNCTION public.safeact_reject_mutation()
-- safeact:statement --
CREATE TRIGGER "safeact_legal_transitions_write_guard" BEFORE UPDATE OR DELETE ON "public"."safeact_legal_transitions" FOR EACH ROW EXECUTE FUNCTION public.safeact_reject_mutation()
-- safeact:statement --

CREATE VIEW "public"."safeact_operation_current_state" AS
SELECT
    o.operation_id,
    o.tenant_id,
    o.created_at,
    COALESCE(t.seq, 0) AS seq,
    COALESCE(t.to_state, 'RECORDED') AS state,
    t.claim_id,
    t.attempt_id,
    t.actor,
    t.lease_expires_at,
    t.created_at AS state_changed_at
FROM "public".safeact_operations o
LEFT JOIN LATERAL (
    SELECT x.seq, x.to_state, x.claim_id, x.attempt_id,
           x.actor, x.lease_expires_at, x.created_at
    FROM "public".safeact_operation_transitions x
    WHERE x.operation_id = o.operation_id
    ORDER BY x.seq DESC
    LIMIT 1
) t ON TRUE

-- safeact:statement --
CREATE FUNCTION "public"."safeact_lifecycle_lock"(p_operation uuid, OUT o_seq integer, OUT o_state text, OUT o_claim uuid, OUT o_attempt uuid, OUT o_actor text, OUT o_lease timestamptz) RETURNS record LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $safeact_lifecycle$ 
BEGIN
    PERFORM 1 FROM "public".safeact_operations o
    WHERE o.operation_id = p_operation
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Unknown operation'
            USING ERRCODE = 'SAT05';
    END IF;

    SELECT t.seq, t.to_state, t.claim_id, t.attempt_id,
           t.actor, t.lease_expires_at
    INTO o_seq, o_state, o_claim, o_attempt, o_actor, o_lease
    FROM "public".safeact_operation_transitions t
    WHERE t.operation_id = p_operation
    ORDER BY t.seq DESC
    LIMIT 1;

    IF NOT FOUND THEN
        o_seq := 0;
        o_state := 'RECORDED';
    END IF;
END; $safeact_lifecycle$
-- safeact:statement --
CREATE FUNCTION "public"."safeact_lifecycle_append"(p_operation uuid, p_seq integer, p_from text, p_to text, p_claim uuid, p_attempt uuid, p_lease timestamptz, p_reason text, p_evidence jsonb, p_event text) RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $safeact_lifecycle$ 
BEGIN
    IF p_reason IS NULL OR length(p_reason) NOT BETWEEN 1 AND 255
       OR p_evidence IS NULL
       OR jsonb_typeof(p_evidence) <> 'object' THEN
        RAISE EXCEPTION 'Invalid transition reason or evidence'
            USING ERRCODE = '22023';
    END IF;

    PERFORM 1 FROM "public".safeact_legal_transitions l
    WHERE l.from_state = p_from AND l.to_state = p_to;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Illegal transition % -> %', p_from, p_to
            USING ERRCODE = 'SAT01';
    END IF;

    -- Outcome claims need recorded proof, on every path. A timeout,
    -- empty lookup, NOT_FOUND or INCONCLUSIVE result never proves
    -- non-application, so it can never reach FAILED_CONFIRMED.
    IF p_to = 'FAILED_CONFIRMED' AND (
        NOT EXISTS (
            SELECT 1 FROM "public".safeact_attempts a
            WHERE a.operation_id = p_operation
        )
        OR EXISTS (
            SELECT 1 FROM "public".safeact_attempts a
            WHERE a.operation_id = p_operation
              AND NOT EXISTS (
                  SELECT 1 FROM "public".safeact_attempt_outcomes x
                  WHERE x.attempt_id = a.attempt_id
                    AND x.outcome = 'proven_not_applied'
                    AND x.evidence_kind = 'proven_not_applied'
                    AND x.evidence_reference IS NOT NULL
              )
        )
        OR EXISTS (
            SELECT 1 FROM "public".safeact_attempt_outcomes x
            WHERE x.operation_id = p_operation
              AND (x.outcome = 'confirmed_applied'
                   OR x.evidence_kind = 'applied')
        )
    ) THEN
        RAISE EXCEPTION
            'Authoritative non-application evidence required'
            USING ERRCODE = 'SAT09';
    END IF;

    IF p_to = 'SUCCEEDED' AND NOT EXISTS (
        SELECT 1 FROM "public".safeact_attempt_outcomes x
        WHERE x.operation_id = p_operation
          AND x.outcome = 'confirmed_applied'
          AND x.evidence_kind = 'applied'
          AND x.evidence_reference IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'Applied-effect evidence required'
            USING ERRCODE = 'SAT09';
    END IF;

    INSERT INTO "public".safeact_operation_transitions (
        operation_id, seq, from_state, to_state, claim_id,
        attempt_id, actor, lease_expires_at, reason, evidence
    ) VALUES (
        p_operation, p_seq, p_from, p_to, p_claim, p_attempt,
        session_user::text, p_lease, p_reason, p_evidence
    );

    INSERT INTO "public".safeact_audit_events (
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
END; $safeact_lifecycle$
-- safeact:statement --
CREATE FUNCTION "public"."safeact_claim"(p_operation uuid, p_lease_seconds integer) RETURNS TABLE(claim_id uuid, seq integer, tenant_id text, action_type text, resource_id text, provider_account_id text, connector_id text, contract_version text, payload_sha256 text, payload_canonical text, provider_idempotency_key text) LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $safeact_lifecycle$ 
DECLARE
    v record;
    v_claim uuid := gen_random_uuid();
BEGIN
    IF p_lease_seconds IS NULL
       OR p_lease_seconds NOT BETWEEN 1 AND 3600 THEN
        RAISE EXCEPTION 'Invalid lease' USING ERRCODE = '22023';
    END IF;

    SELECT * INTO v FROM "public".safeact_lifecycle_lock(p_operation);

    -- Milestone 1 claims only never-claimed RECORDED work.
    IF v.o_state = 'RETRY_ELIGIBLE' THEN
        RAISE EXCEPTION 'Reclaim is disabled in Milestone 1'
            USING ERRCODE = 'SAT06';
    END IF;

    IF v.o_seq <> 0 THEN
        RAISE EXCEPTION 'Operation already claimed'
            USING ERRCODE = 'SAT02';
    END IF;

    PERFORM 1 FROM "public".safeact_outbox_events e
    WHERE e.operation_id = p_operation
      AND e.event_type = 'INTENT_RECORDED_NO_DISPATCH'
      AND e.state = 'PENDING';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Admission bundle incomplete'
            USING ERRCODE = 'SAT08';
    END IF;

    PERFORM "public".safeact_lifecycle_append(
        p_operation, 1, 'RECORDED', 'CLAIMED', v_claim, NULL,
        clock_timestamp() + make_interval(secs => p_lease_seconds),
        'claimed', '{}'::jsonb, 'LIFECYCLE_CLAIMED'
    );

    -- Dispatch inputs come from the immutable durable record read
    -- under this lock, never from the delivered task.
    RETURN QUERY
    SELECT v_claim, 1,
           o.tenant_id::text, o.action_type::text,
           o.resource_id::text, o.provider_account_id::text,
           o.connector_id::text, o.contract_version::text,
           o.payload_sha256::text, o.payload_canonical,
           o.provider_idempotency_key::text
    FROM "public".safeact_operations o
    WHERE o.operation_id = p_operation;
END; $safeact_lifecycle$
-- safeact:statement --
CREATE FUNCTION "public"."safeact_begin_dispatch"(p_operation uuid, p_expected_seq integer, p_claim uuid, p_lease_seconds integer) RETURNS TABLE(seq integer, attempt_id uuid) LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $safeact_lifecycle$ 
DECLARE
    v record;
    v_attempt uuid := gen_random_uuid();
    v_ordinal integer;
BEGIN
    IF p_lease_seconds IS NULL
       OR p_lease_seconds NOT BETWEEN 1 AND 3600 THEN
        RAISE EXCEPTION 'Invalid lease' USING ERRCODE = '22023';
    END IF;

    SELECT * INTO v FROM "public".safeact_lifecycle_lock(p_operation);

    IF v.o_seq IS DISTINCT FROM p_expected_seq THEN
        RAISE EXCEPTION 'Stale owner version'
            USING ERRCODE = 'SAT02';
    END IF;

    IF v.o_state <> 'CLAIMED' THEN
        RAISE EXCEPTION 'Illegal transition % -> DISPATCHING',
            v.o_state USING ERRCODE = 'SAT01';
    END IF;

    IF v.o_claim IS DISTINCT FROM p_claim
       OR v.o_actor IS DISTINCT FROM session_user::text THEN
        RAISE EXCEPTION 'Not the claim owner'
            USING ERRCODE = 'SAT03';
    END IF;

    IF v.o_lease <= clock_timestamp() THEN
        RAISE EXCEPTION 'Claim lease expired before dispatch'
            USING ERRCODE = 'SAT07';
    END IF;

    SELECT count(*) + 1 INTO v_ordinal
    FROM "public".safeact_attempts a
    WHERE a.operation_id = p_operation;

    INSERT INTO "public".safeact_attempts (
        attempt_id, operation_id, ordinal, dispatch_risk
    ) VALUES (v_attempt, p_operation, v_ordinal, 'PREPARED');

    -- Committing this row is the dispatch-risk fence: from here
    -- the provider call may have happened.
    PERFORM "public".safeact_lifecycle_append(
        p_operation, v.o_seq + 1, 'CLAIMED', 'DISPATCHING',
        p_claim, v_attempt,
        clock_timestamp() + make_interval(secs => p_lease_seconds),
        'dispatch_risk_committed', '{}'::jsonb,
        'DISPATCH_RISK_COMMITTED'
    );

    RETURN QUERY SELECT v.o_seq + 1, v_attempt;
END; $safeact_lifecycle$
-- safeact:statement --
CREATE FUNCTION "public"."safeact_record_dispatch_outcome"(p_operation uuid, p_expected_seq integer, p_claim uuid, p_to_state text, p_reason text, p_evidence jsonb) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $safeact_lifecycle$ 
DECLARE
    v record;
BEGIN
    SELECT * INTO v FROM "public".safeact_lifecycle_lock(p_operation);

    IF v.o_seq IS DISTINCT FROM p_expected_seq THEN
        RAISE EXCEPTION 'Stale owner version'
            USING ERRCODE = 'SAT02';
    END IF;

    IF v.o_state <> 'DISPATCHING'
       OR p_to_state IS NULL
       OR p_to_state NOT IN ('FAILED_CONFIRMED', 'SUCCEEDED', 'UNKNOWN') THEN
        RAISE EXCEPTION 'Illegal transition % -> %',
            v.o_state, p_to_state USING ERRCODE = 'SAT01';
    END IF;

    IF v.o_claim IS DISTINCT FROM p_claim
       OR v.o_actor IS DISTINCT FROM session_user::text THEN
        RAISE EXCEPTION 'Not the claim owner'
            USING ERRCODE = 'SAT03';
    END IF;

    PERFORM "public".safeact_lifecycle_append(
        p_operation, v.o_seq + 1, 'DISPATCHING', p_to_state,
        v.o_claim, v.o_attempt, NULL, p_reason, p_evidence,
        'LIFECYCLE_DISPATCH_OUTCOME'
    );

    RETURN v.o_seq + 1;
END; $safeact_lifecycle$
-- safeact:statement --
CREATE FUNCTION "public"."safeact_recovery_transition"(p_operation uuid, p_expected_seq integer, p_expected_state text, p_to_state text, p_reason text, p_evidence jsonb) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $safeact_lifecycle$ 
DECLARE
    v record;
BEGIN
    SELECT * INTO v FROM "public".safeact_lifecycle_lock(p_operation);

    IF v.o_seq IS DISTINCT FROM p_expected_seq
       OR v.o_state IS DISTINCT FROM p_expected_state THEN
        RAISE EXCEPTION 'Stale owner version or state'
            USING ERRCODE = 'SAT02';
    END IF;

    IF (p_expected_state, p_to_state) NOT IN (('DISPATCHING', 'UNKNOWN'), ('UNKNOWN', 'VERIFYING'), ('VERIFYING', 'ESCALATED_UNRESOLVED'), ('VERIFYING', 'FAILED_CONFIRMED'), ('VERIFYING', 'RETRY_ELIGIBLE'), ('VERIFYING', 'SUCCEEDED'), ('VERIFYING', 'UNKNOWN')) THEN
        RAISE EXCEPTION 'Illegal recovery transition % -> %',
            p_expected_state, p_to_state USING ERRCODE = 'SAT01';
    END IF;

    -- An expired lease permits investigation, never dispatch.
    IF p_expected_state = 'DISPATCHING'
       AND v.o_lease > clock_timestamp() THEN
        RAISE EXCEPTION 'Dispatch lease has not expired'
            USING ERRCODE = 'SAT04';
    END IF;

    PERFORM "public".safeact_lifecycle_append(
        p_operation, v.o_seq + 1, p_expected_state, p_to_state,
        v.o_claim, v.o_attempt, NULL, p_reason, p_evidence,
        'LIFECYCLE_RECOVERY'
    );

    RETURN v.o_seq + 1;
END; $safeact_lifecycle$
-- safeact:statement --
CREATE FUNCTION "public"."safeact_record_attempt_outcome"(p_attempt uuid, p_outcome text, p_evidence_kind text, p_reference text, p_details jsonb) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $safeact_lifecycle$ 
DECLARE
    v record;
    v_operation uuid;
    v_id uuid := gen_random_uuid();
BEGIN
    SELECT a.operation_id INTO v_operation
    FROM "public".safeact_attempts a
    WHERE a.attempt_id = p_attempt;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Unknown attempt'
            USING ERRCODE = 'SAT05';
    END IF;

    -- Evidence is appended, never replacing earlier evidence or
    -- the current decision; observed_seq records what it saw.
    SELECT * INTO v FROM "public".safeact_lifecycle_lock(v_operation);

    INSERT INTO "public".safeact_attempt_outcomes (
        outcome_id, operation_id, attempt_id, outcome,
        evidence_kind, evidence_reference, observed_by,
        observed_seq, details
    ) VALUES (
        v_id, v_operation, p_attempt, p_outcome, p_evidence_kind,
        p_reference, session_user::text, v.o_seq,
        COALESCE(p_details, '{}'::jsonb)
    );

    INSERT INTO "public".safeact_audit_events (
        operation_id, event_type, actor_id, details
    ) VALUES (
        v_operation, 'ATTEMPT_EVIDENCE_RECORDED', session_user::text,
        jsonb_build_object(
            'schema_version', 1,
            'outcome_id', v_id,
            'attempt_id', p_attempt,
            'outcome', p_outcome,
            'observed_seq', v.o_seq
        )
    );

    RETURN v_id;
END; $safeact_lifecycle$
-- safeact:statement --
CREATE FUNCTION "public"."safeact_record_outbox_delivery"(p_outbox uuid) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $safeact_lifecycle$ 
DECLARE
    v_operation uuid;
    v_id uuid := gen_random_uuid();
BEGIN
    SELECT e.operation_id INTO v_operation
    FROM "public".safeact_outbox_events e
    WHERE e.outbox_id = p_outbox;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Unknown outbox event'
            USING ERRCODE = 'SAT05';
    END IF;

    -- Deliveries are at-least-once: many rows per event allowed.
    INSERT INTO "public".safeact_outbox_deliveries (
        delivery_id, outbox_id, operation_id, published_by
    ) VALUES (v_id, p_outbox, v_operation, session_user::text);

    RETURN v_id;
END; $safeact_lifecycle$
-- safeact:statement --
CREATE FUNCTION "public"."safeact_claimable_operations"(p_limit integer) RETURNS TABLE(operation_id uuid) STABLE LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $safeact_lifecycle$ 
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION 'Invalid page size' USING ERRCODE = '22023';
    END IF;

    -- Never-claimed work only: RETRY_ELIGIBLE is not claimable
    -- in Milestone 1.
    RETURN QUERY 
    SELECT o.operation_id
    FROM "public".safeact_operations o
    WHERE NOT EXISTS (
        SELECT 1 FROM "public".safeact_operation_transitions t
        WHERE t.operation_id = o.operation_id
    )
    ORDER BY o.created_at, o.operation_id
    LIMIT p_limit;
END; $safeact_lifecycle$
-- safeact:statement --
CREATE FUNCTION "public"."safeact_relay_pending"(p_limit integer) RETURNS TABLE(outbox_id uuid, operation_id uuid) STABLE LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $safeact_lifecycle$ 
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION 'Invalid page size' USING ERRCODE = '22023';
    END IF;

    -- Outbox events whose operation was never claimed. The relay
    -- may publish one more than once; workers tolerate duplicates.
    RETURN QUERY
    SELECT e.outbox_id, e.operation_id
    FROM (
    SELECT o.operation_id
    FROM "public".safeact_operations o
    WHERE NOT EXISTS (
        SELECT 1 FROM "public".safeact_operation_transitions t
        WHERE t.operation_id = o.operation_id
    )
    ORDER BY o.created_at, o.operation_id
    LIMIT p_limit) c
    JOIN "public".safeact_outbox_events e
      ON e.operation_id = c.operation_id
     AND e.event_type = 'INTENT_RECORDED_NO_DISPATCH'
     AND e.state = 'PENDING';
END; $safeact_lifecycle$
-- safeact:statement --
CREATE FUNCTION "public"."safeact_operations_in_state"(p_state text, p_limit integer) RETURNS TABLE(operation_id uuid, seq integer) STABLE LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $safeact_lifecycle$ 
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 1000
       OR p_state IS NULL OR p_state = 'RECORDED' THEN
        RAISE EXCEPTION 'Invalid state scan' USING ERRCODE = '22023';
    END IF;

    RETURN QUERY 
    SELECT t.operation_id, t.seq
    FROM "public".safeact_operation_transitions t
    WHERE t.to_state = p_state
      AND NOT EXISTS (
          SELECT 1 FROM "public".safeact_operation_transitions n
          WHERE n.operation_id = t.operation_id
            AND n.seq > t.seq
      )
    ORDER BY t.operation_id
    LIMIT p_limit;
END; $safeact_lifecycle$
-- safeact:statement --
CREATE FUNCTION "public"."safeact_recovery_snapshot"(p_operation uuid) RETURNS TABLE(seq integer, state text, tenant_id text, action_type text, resource_id text, provider_account_id text, connector_id text, contract_version text, payload_sha256 text, provider_idempotency_key text, first_dispatch_at timestamptz, lease_expires_at timestamptz, observed_at timestamptz, attempts jsonb) STABLE LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $safeact_lifecycle$ 
BEGIN
    -- Read-only inputs for the pure recovery policy. Attempt
    -- inventory is complete by construction: attempts are created
    -- only by safeact_begin_dispatch.
    RETURN QUERY
    SELECT c.seq, c.state::text,
           o.tenant_id::text, o.action_type::text,
           o.resource_id::text, o.provider_account_id::text,
           o.connector_id::text, o.contract_version::text,
           o.payload_sha256::text, o.provider_idempotency_key::text,
           (SELECT min(t.created_at)
            FROM "public".safeact_operation_transitions t
            WHERE t.operation_id = o.operation_id
              AND t.to_state = 'DISPATCHING'),
           c.lease_expires_at,
           clock_timestamp(),
           COALESCE((
               SELECT jsonb_agg(jsonb_build_object(
                   'attempt_id', a.attempt_id,
                   'ordinal', a.ordinal,
                   'applied_reference', (
                       SELECT max(x.evidence_reference)
                       FROM "public".safeact_attempt_outcomes x
                       WHERE x.attempt_id = a.attempt_id
                         AND x.outcome = 'confirmed_applied'
                         AND x.evidence_kind = 'applied'),
                   'not_applied_reference', (
                       SELECT max(x.evidence_reference)
                       FROM "public".safeact_attempt_outcomes x
                       WHERE x.attempt_id = a.attempt_id
                         AND x.outcome = 'proven_not_applied'
                         AND x.evidence_kind = 'proven_not_applied')
               ) ORDER BY a.ordinal)
               FROM "public".safeact_attempts a
               WHERE a.operation_id = o.operation_id
           ), '[]'::jsonb)
    FROM "public".safeact_operations o
    JOIN "public"."safeact_operation_current_state" c ON c.operation_id = o.operation_id
    WHERE o.operation_id = p_operation;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Unknown operation'
            USING ERRCODE = 'SAT05';
    END IF;
END; $safeact_lifecycle$
-- safeact:statement --
REVOKE ALL ON FUNCTION "public"."safeact_claim"(uuid, integer) FROM PUBLIC
-- safeact:statement --
REVOKE ALL ON FUNCTION "public"."safeact_begin_dispatch"(uuid, integer, uuid, integer) FROM PUBLIC
-- safeact:statement --
REVOKE ALL ON FUNCTION "public"."safeact_record_dispatch_outcome"(uuid, integer, uuid, text, text, jsonb) FROM PUBLIC
-- safeact:statement --
REVOKE ALL ON FUNCTION "public"."safeact_record_outbox_delivery"(uuid) FROM PUBLIC
-- safeact:statement --
REVOKE ALL ON FUNCTION "public"."safeact_claimable_operations"(integer) FROM PUBLIC
-- safeact:statement --
REVOKE ALL ON FUNCTION "public"."safeact_relay_pending"(integer) FROM PUBLIC
-- safeact:statement --
REVOKE ALL ON FUNCTION "public"."safeact_recovery_transition"(uuid, integer, text, text, text, jsonb) FROM PUBLIC
-- safeact:statement --
REVOKE ALL ON FUNCTION "public"."safeact_operations_in_state"(text, integer) FROM PUBLIC
-- safeact:statement --
REVOKE ALL ON FUNCTION "public"."safeact_recovery_snapshot"(uuid) FROM PUBLIC
-- safeact:statement --
REVOKE ALL ON FUNCTION "public"."safeact_record_attempt_outcome"(uuid, text, text, text, jsonb) FROM PUBLIC
-- safeact:statement --
REVOKE ALL ON FUNCTION "public"."safeact_lifecycle_lock"(uuid) FROM PUBLIC
-- safeact:statement --
REVOKE ALL ON FUNCTION "public"."safeact_lifecycle_append"(uuid, integer, text, text, uuid, uuid, timestamp with time zone, text, jsonb, text) FROM PUBLIC
