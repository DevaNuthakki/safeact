"""FROZEN once committed: SQL for revision 0013_review.

Version-bound human review of proposed next actions (Milestone 3, F14).

- An investigator proposes one permitted next action for an operation.
  The server decides eligibility and snapshots the binding: operation
  version (seq) and state, payload hash, a digest of every recorded
  evidence row, the contract, the action (and, for an attestation, the
  reference), and an expiry. Its SHA-256 is the binding digest.
- A reviewer decides. Approval requires a human reviewer who is neither
  the proposer nor the operation's requester (SAT25), an undecided
  proposal (SAT24), and a binding that still matches the operation now
  and the digest the reviewer's screen showed. A stale or expired
  binding is recorded (APPROVAL_REFUSED_STALE) and refused as a result,
  not an error, so the audit row is kept. One decision per proposal,
  serialized by the operation lock; a repeated click with the same
  decision key returns the stored decision.
- Approved actions, each still under every earlier SQL guard:
  REVERIFY: one extra post-escalation lookup beyond the contract's
    interval and horizon, consumed once (safeact_take_reverify); a
    reopen still needs decisive evidence.
  PROTECTED_RETRY: for a contract whose review policy requires it, the
    approval itself moves VERIFYING -> RETRY_ELIGIBLE; the append
    function refuses that move without a live approval bound to the
    current seq (SAT27), on top of the retry basis and attempt budget.
  ATTEST_APPLIED: records the reviewer's applied-effect reference as a
    confirmed_applied attempt outcome with provenance 'human_attested'.
    A CHECK allows human provenance on confirmed_applied rows only, so a
    human can never supply proof of non-application (blueprint 04).
- safeact_succeeded_by_provenance reports provider-verified and
  human-attested successes apart.

Pinned by tests/phase_a/test_migration_sql_frozen.py. Values are
literals; nothing is read from core.
"""

from psycopg import sql

from storage.lifecycle_sql import v0003, v0011, v0012


REVISION = "0013_review"

ALREADY_DECIDED = "SAT24"
SELF_REVIEW = "SAT25"
NOT_ELIGIBLE = "SAT26"
APPROVAL_REQUIRED = "SAT27"

ACTIONS = ("ATTEST_APPLIED", "PROTECTED_RETRY", "REVERIFY")
PROPOSAL_TTL = "15 minutes"
MAX_RATIONALE = 2000
DIGEST_DOMAIN = "safeact-proposal-v1"

NEW_TABLES = (
    "safeact_contract_review_policies",
    "safeact_proposals",
    "safeact_decisions",
    "safeact_reverify_uses",
)
HISTORY_TABLES = tuple(reversed(NEW_TABLES))
NEW_INDEXES = ("ix_safeact_proposals_operation",)
LEGAL_TRANSITIONS = frozenset()

NEW_FUNCTIONS = {
    "safeact_evidence_digest": "uuid",
    "safeact_retry_approved": "uuid, integer",
    "safeact_proposal_eligibility": "uuid, text, text",
    "safeact_proposal_digest": "uuid, text, text, timestamp with time zone",
    "safeact_proposal_view": "uuid",
    "safeact_api_propose": "text, text, uuid, text, text, text",
    "safeact_api_proposals": "text, text, integer",
    "safeact_api_proposal": "text, uuid",
    "safeact_api_decide": "text, text, uuid, text, text, uuid, text",
    "safeact_retry_approval_status": "uuid",
    "safeact_take_reverify": "uuid",
    "safeact_succeeded_by_provenance": "",
}
REPLACED_FUNCTIONS = (
    "safeact_lifecycle_append",
    "safeact_api_operation",
)

SHARED_FUNCTIONS = {}
WORKER_FUNCTIONS = {}
ADVANCER_FUNCTIONS = {}
INTERNAL_FUNCTIONS = {
    "safeact_evidence_digest": "uuid",
    "safeact_retry_approved": "uuid, integer",
    "safeact_proposal_eligibility": "uuid, text, text",
    "safeact_proposal_digest": "uuid, text, text, timestamp with time zone",
    "safeact_proposal_view": "uuid",
}
API_FUNCTIONS = {
    "safeact_api_propose": "text, text, uuid, text, text, text",
    "safeact_api_proposals": "text, text, integer",
    "safeact_api_proposal": "text, uuid",
    "safeact_api_decide": "text, text, uuid, text, text, uuid, text",
}
RECOVERY_FUNCTIONS = {
    "safeact_retry_approval_status": "uuid",
    "safeact_take_reverify": "uuid",
    "safeact_succeeded_by_provenance": "",
}
OWNER_TABLE_PRIVILEGES = {
    "safeact_contract_review_policies": ("SELECT",),
    "safeact_proposals": ("SELECT", "INSERT"),
    "safeact_decisions": ("SELECT", "INSERT"),
    "safeact_reverify_uses": ("SELECT", "INSERT"),
    "safeact_attempt_outcomes": ("SELECT", "INSERT"),
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
        role_required=sql.Literal(v0011.ROLE_REQUIRED),
        invalid=sql.Literal(v0012.REQUEST_INVALID),
        decided=sql.Literal(ALREADY_DECIDED),
        self_review=sql.Literal(SELF_REVIEW),
        not_eligible=sql.Literal(NOT_ELIGIBLE),
        approval_required=sql.Literal(APPROVAL_REQUIRED),
        ttl=sql.Literal(PROPOSAL_TTL),
        max_rationale=sql.Literal(MAX_RATIONALE),
        domain=sql.Literal(DIGEST_DOMAIN),
        max_page=sql.Literal(v0003.MAX_PAGE),
        view=sql.Identifier(v0003.CURRENT_STATE_VIEW),
    )


def _tables(s):
    actions = ", ".join(f"'{a}'" for a in ACTIONS)
    outcomes = sql.SQL("{}.safeact_attempt_outcomes").format(s)
    return [
        sql.SQL("""
CREATE TABLE {s}.safeact_contract_review_policies (
    connector_id varchar(64) NOT NULL,
    version varchar(64) NOT NULL,
    retry_requires_approval boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_safeact_contract_review_policies
        PRIMARY KEY (connector_id, version),
    CONSTRAINT fk_safeact_contract_review_policies_contract
        FOREIGN KEY (connector_id, version)
        REFERENCES {s}.safeact_connector_contracts (connector_id, version)
        ON DELETE RESTRICT
)""").format(s=s),

        sql.SQL("""
CREATE TABLE {s}.safeact_proposals (
    proposal_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL
        REFERENCES {s}.safeact_operations (operation_id)
        ON DELETE RESTRICT,
    tenant_id varchar(128) NOT NULL,
    action varchar(32) NOT NULL,
    rationale text NOT NULL,
    attested_reference varchar(255),
    proposed_by uuid NOT NULL
        REFERENCES {s}.safeact_principals (principal_id)
        ON DELETE RESTRICT,
    bound_seq integer NOT NULL,
    bound_state varchar(32) NOT NULL,
    bound_payload_sha256 char(64) NOT NULL,
    bound_evidence_digest char(64) NOT NULL,
    bound_connector_id varchar(64) NOT NULL,
    bound_contract_version varchar(64) NOT NULL,
    binding_digest char(64) NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_safeact_proposals_action CHECK (action IN (""" + actions + """)),
    CONSTRAINT ck_safeact_proposals_reference CHECK (
        (action = 'ATTEST_APPLIED') = (attested_reference IS NOT NULL)
    ),
    CONSTRAINT ck_safeact_proposals_text CHECK (
        length(rationale) BETWEEN 1 AND {max_rationale}
    ),
    CONSTRAINT ck_safeact_proposals_digests CHECK (
        bound_evidence_digest ~ '^[0-9a-f]{{64}}$'
        AND binding_digest ~ '^[0-9a-f]{{64}}$'
    ),
    CONSTRAINT ck_safeact_proposals_expiry CHECK (expires_at > created_at)
)""").format(s=s, max_rationale=sql.Literal(MAX_RATIONALE)),

        sql.SQL("""
CREATE TABLE {s}.safeact_decisions (
    proposal_id uuid PRIMARY KEY
        REFERENCES {s}.safeact_proposals (proposal_id)
        ON DELETE RESTRICT,
    decision varchar(16) NOT NULL,
    decided_by uuid NOT NULL
        REFERENCES {s}.safeact_principals (principal_id)
        ON DELETE RESTRICT,
    decision_key uuid NOT NULL,
    note text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_safeact_decisions_key UNIQUE (decision_key),
    CONSTRAINT ck_safeact_decisions_decision
        CHECK (decision IN ('approve', 'reject')),
    CONSTRAINT ck_safeact_decisions_note
        CHECK (note IS NULL OR length(note) <= {max_rationale})
)""").format(s=s, max_rationale=sql.Literal(MAX_RATIONALE)),

        sql.SQL("""
CREATE TABLE {s}.safeact_reverify_uses (
    proposal_id uuid PRIMARY KEY
        REFERENCES {s}.safeact_proposals (proposal_id)
        ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now()
)""").format(s=s),

        sql.SQL(
            "CREATE INDEX ix_safeact_proposals_operation "
            "ON {s}.safeact_proposals (operation_id, created_at)"
        ).format(s=s),

        # Human provenance exists only for an applied-effect reference.
        sql.SQL(
            "ALTER TABLE {} ADD COLUMN provenance varchar(16) "
            "NOT NULL DEFAULT 'system'"
        ).format(outcomes),
        sql.SQL(
            "ALTER TABLE {} ADD CONSTRAINT ck_safeact_attempt_outcomes_provenance "
            "CHECK (provenance IN ('system', 'human_attested') AND ("
            "provenance = 'system' OR (outcome = 'confirmed_applied' "
            "AND evidence_kind = 'applied' AND evidence_reference IS NOT NULL)))"
        ).format(outcomes),
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
    # The frozen 0012 append, plus: a policy that requires review makes
    # VERIFYING -> RETRY_ELIGIBLE need a live approval for this seq.
    base = v0012._append(s).as_string(None)
    marker = (
        "    INSERT INTO " + s.as_string(None)
        + ".safeact_operation_transitions ("
    )
    if base.count(marker) != 1:
        raise RuntimeError("0012 append no longer has one transition insert")
    head, tail = base.split(marker)
    guard = sql.SQL("""    -- 0013: a contract whose review policy requires it retries only
    -- under a live human approval bound to the operation's current seq.
    IF p_from = 'VERIFYING' AND p_to = 'RETRY_ELIGIBLE' AND EXISTS (
        SELECT 1
        FROM {s}.safeact_operations o
        JOIN {s}.safeact_contract_review_policies r
          ON r.connector_id = o.connector_id
         AND r.version = o.contract_version
        WHERE o.operation_id = p_operation
          AND r.retry_requires_approval
    ) AND NOT {s}.safeact_retry_approved(p_operation, p_seq - 1) THEN
        RAISE EXCEPTION 'Retry needs a reviewer approval for this version'
            USING ERRCODE = {approval_required};
    END IF;

""").format(
        s=s, approval_required=sql.Literal(APPROVAL_REQUIRED),
    ).as_string(None)
    return sql.SQL(head + guard + marker + tail)


def _operation(s):
    # The frozen 0012 detail view, plus each evidence row's provenance and
    # how a success is known: provider-verified or human-attested only.
    base = v0012._operation(s).as_string(None)
    row = "'observed_seq', x.observed_seq,"
    top = "'outcome_class', CASE"
    if base.count(row) != 1 or base.count(top) != 1:
        raise RuntimeError("0012 operation view changed shape")
    base = base.replace(row, row + "\n                        'provenance', x.provenance,")
    base = base.replace(top, sql.SQL("""'success_provenance', CASE
            WHEN v_state.state <> 'SUCCEEDED' THEN NULL
            WHEN EXISTS (
                SELECT 1 FROM {s}.safeact_attempt_outcomes y
                WHERE y.operation_id = o.operation_id
                  AND y.outcome = 'confirmed_applied'
                  AND y.evidence_kind = 'applied'
                  AND y.provenance = 'system'
            ) THEN 'provider_verified'
            ELSE 'human_attested' END,
        """).format(s=s).as_string(None) + top)
    return sql.SQL("CREATE OR REPLACE" + base[len("CREATE"):])


def _evidence_digest(s):
    return _function(s, "safeact_evidence_digest", "p_operation uuid",
                     "text", """
BEGIN
    RETURN encode(sha256(convert_to(coalesce((
        SELECT string_agg(
            x.outcome_id::text || ':' || x.outcome || ':'
            || coalesce(x.evidence_kind, '') || ':'
            || coalesce(x.evidence_reference, '') || ':' || x.provenance,
            ',' ORDER BY x.created_at, x.outcome_id)
        FROM {s}.safeact_attempt_outcomes x
        WHERE x.operation_id = p_operation
    ), ''), 'UTF8')), 'hex');
END;
""", definer=False, volatility="STABLE")


def _retry_approved(s):
    return _function(
        s, "safeact_retry_approved", "p_operation uuid, p_seq integer",
        "boolean", """
BEGIN
    RETURN EXISTS (
        SELECT 1
        FROM {s}.safeact_proposals p
        JOIN {s}.safeact_decisions d ON d.proposal_id = p.proposal_id
        WHERE p.operation_id = p_operation
          AND p.action = 'PROTECTED_RETRY'
          AND p.bound_seq = p_seq
          AND p.expires_at > clock_timestamp()
          AND d.decision = 'approve'
    );
END;
""", definer=False, volatility="STABLE")


def _eligibility(s):
    # NULL when the action is a permitted next action now; otherwise why
    # not. Every rule is re-checked by the guarded function that acts.
    return _function(
        s, "safeact_proposal_eligibility",
        "p_operation uuid, p_action text, p_reference text", "text", """
DECLARE
    v_state text;
    v_seq integer;
    v_attempts integer;
    v_lookup text;
    v_policy boolean;
    v_basis text;
BEGIN
    SELECT c.state, c.seq INTO v_state, v_seq
      FROM {s}.{view} c WHERE c.operation_id = p_operation;
    SELECT count(*) INTO v_attempts
      FROM {s}.safeact_attempts a WHERE a.operation_id = p_operation;
    SELECT k.lookup_semantics, coalesce(r.retry_requires_approval, false)
      INTO v_lookup, v_policy
      FROM {s}.safeact_operations o
      JOIN {s}.safeact_connector_contracts k
        ON k.connector_id = o.connector_id AND k.version = o.contract_version
      LEFT JOIN {s}.safeact_contract_review_policies r
        ON r.connector_id = o.connector_id AND r.version = o.contract_version
     WHERE o.operation_id = p_operation;

    IF p_action NOT IN ('REVERIFY', 'PROTECTED_RETRY', 'ATTEST_APPLIED') THEN
        RETURN 'unknown_action';
    END IF;
    IF v_attempts = 0 THEN
        RETURN 'no_attempts';
    END IF;

    IF p_action = 'REVERIFY' THEN
        IF v_state IS DISTINCT FROM 'ESCALATED_UNRESOLVED' THEN
            RETURN 'wrong_state';
        ELSIF v_lookup IS NULL OR v_lookup = 'unavailable' THEN
            RETURN 'lookup_unavailable';
        END IF;
    ELSIF p_action = 'ATTEST_APPLIED' THEN
        IF v_state IS DISTINCT FROM 'ESCALATED_UNRESOLVED' THEN
            RETURN 'wrong_state';
        ELSIF p_reference IS NULL
              OR length(p_reference) NOT BETWEEN 1 AND 255
              OR p_reference ~ '[[:cntrl:]]'
              OR p_reference <> btrim(p_reference) THEN
            RETURN 'reference_required';
        END IF;
    ELSE
        IF v_state IS DISTINCT FROM 'VERIFYING' THEN
            RETURN 'wrong_state';
        ELSIF NOT v_policy THEN
            RETURN 'approval_not_required';
        END IF;
        v_basis := {s}.safeact_retry_basis(p_operation);
        IF v_basis IS DISTINCT FROM 'proof'
           AND v_basis IS DISTINCT FROM 'same_key' THEN
            RETURN 'retry_not_protected';
        ELSIF v_attempts >= {s}.safeact_attempt_budget(p_operation) THEN
            RETURN 'attempt_budget_exhausted';
        END IF;
    END IF;

    IF p_action <> 'ATTEST_APPLIED' AND p_reference IS NOT NULL THEN
        RETURN 'reference_not_allowed';
    END IF;
    RETURN NULL;
END;
""", definer=False, volatility="STABLE")


def _digest(s):
    # The binding the reviewer approves, recomputed from the database's
    # current values: any change to the operation, its evidence, its
    # contract or the proposal makes the digest differ.
    return _function(
        s, "safeact_proposal_digest",
        "p_operation uuid, p_action text, p_reference text, "
        "p_expires timestamptz",
        "TABLE (seq integer, state text, payload_sha256 text, "
        "evidence_digest text, connector_id text, contract_version text, "
        "digest text)", """
#variable_conflict use_column
DECLARE
    v record;
    v_evidence text;
BEGIN
    SELECT c.seq, c.state::text AS state, o.payload_sha256::text AS payload,
           o.connector_id::text AS connector, o.contract_version::text AS version
      INTO v
      FROM {s}.safeact_operations o
      JOIN {s}.{view} c ON c.operation_id = o.operation_id
     WHERE o.operation_id = p_operation;
    v_evidence := {s}.safeact_evidence_digest(p_operation);

    RETURN QUERY SELECT v.seq, v.state, v.payload, v_evidence, v.connector,
        v.version,
        encode(sha256(convert_to(concat_ws('|',
            {domain}, p_operation::text, p_action, v.seq::text, v.state,
            v.payload, v_evidence, v.connector, v.version,
            coalesce(p_reference, ''),
            to_char(p_expires AT TIME ZONE 'UTC',
                    'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
        ), 'UTF8')), 'hex');
END;
""", definer=False, volatility="STABLE")


def _view_fn(s):
    return _function(s, "safeact_proposal_view", "p_proposal uuid",
                     "jsonb", """
DECLARE
    v_p {s}.safeact_proposals%ROWTYPE;
    v_d {s}.safeact_decisions%ROWTYPE;
    v_now record;
    v_eligible text;
    v_expired boolean;
    v_stale boolean;
    v_status text;
BEGIN
    SELECT * INTO v_p FROM {s}.safeact_proposals p
     WHERE p.proposal_id = p_proposal;
    SELECT * INTO v_d FROM {s}.safeact_decisions d
     WHERE d.proposal_id = p_proposal;
    SELECT * INTO v_now FROM {s}.safeact_proposal_digest(
        v_p.operation_id, v_p.action, v_p.attested_reference, v_p.expires_at
    );
    v_eligible := {s}.safeact_proposal_eligibility(
        v_p.operation_id, v_p.action, v_p.attested_reference
    );
    v_expired := v_p.expires_at <= clock_timestamp();
    v_stale := v_now.digest IS DISTINCT FROM v_p.binding_digest
               OR v_eligible IS NOT NULL;
    v_status := CASE
        WHEN v_d.decision = 'approve' THEN 'approved'
        WHEN v_d.decision = 'reject' THEN 'rejected'
        WHEN v_expired THEN 'expired'
        WHEN v_stale THEN 'stale'
        ELSE 'open' END;

    RETURN jsonb_build_object(
        'proposal_id', v_p.proposal_id,
        'operation_id', v_p.operation_id,
        'action', v_p.action,
        'rationale', v_p.rationale,
        'attested_reference', v_p.attested_reference,
        'proposed_by', v_p.proposed_by,
        'created_at', v_p.created_at,
        'expires_at', v_p.expires_at,
        'status', v_status,
        'binding', jsonb_build_object(
            'seq', v_p.bound_seq,
            'state', v_p.bound_state,
            'payload_sha256', v_p.bound_payload_sha256,
            'evidence_digest', v_p.bound_evidence_digest,
            'connector_id', v_p.bound_connector_id,
            'contract_version', v_p.bound_contract_version
        ),
        'binding_digest', v_p.binding_digest,
        'current', jsonb_build_object(
            'seq', v_now.seq,
            'state', v_now.state,
            'evidence_digest', v_now.evidence_digest,
            'eligibility', v_eligible
        ),
        'decision', CASE WHEN v_d.proposal_id IS NULL THEN NULL
            ELSE jsonb_build_object(
                'decision', v_d.decision,
                'decided_by', v_d.decided_by,
                'note', v_d.note,
                'created_at', v_d.created_at
            ) END
    );
END;
""", definer=False, volatility="STABLE")


def _propose(s):
    return _function(
        s, "safeact_api_propose",
        "p_secret text, p_csrf text, p_operation uuid, p_action text, "
        "p_rationale text, p_reference text",
        "jsonb", """
DECLARE
    v record;
    v_reason text;
    v_expires timestamptz;
    v_deadline timestamptz;
    v_bind record;
    v_id uuid := gen_random_uuid();
BEGIN
    SELECT * INTO v
      FROM {s}.safeact_api_session(p_secret, p_csrf, true, 'investigator');

    IF p_operation IS NULL OR NOT {s}.safeact_api_visible(
        p_operation, v.principal_id, v.tenant_id, v.roles
    ) THEN
        RAISE EXCEPTION 'Unknown operation' USING ERRCODE = {unknown};
    END IF;
    IF p_rationale IS NULL
       OR length(btrim(p_rationale)) NOT BETWEEN 1 AND {max_rationale} THEN
        RAISE EXCEPTION 'Invalid request' USING ERRCODE = {invalid};
    END IF;

    -- Serialize with transitions and decisions on this operation.
    PERFORM 1 FROM {s}.safeact_lifecycle_lock(p_operation);

    v_reason := {s}.safeact_proposal_eligibility(
        p_operation, p_action, p_reference
    );
    IF v_reason IS NULL AND EXISTS (
        SELECT 1 FROM {s}.safeact_proposals p
        JOIN {s}.{view} c ON c.operation_id = p.operation_id
        WHERE p.operation_id = p_operation AND p.action = p_action
          AND p.bound_seq = c.seq AND p.expires_at > clock_timestamp()
          AND NOT EXISTS (
              SELECT 1 FROM {s}.safeact_decisions d
              WHERE d.proposal_id = p.proposal_id
          )
    ) THEN
        v_reason := 'open_proposal_exists';
    END IF;
    IF v_reason IS NOT NULL THEN
        RAISE EXCEPTION 'Not a permitted next action: %', v_reason
            USING ERRCODE = {not_eligible};
    END IF;

    -- A retry approval must land before the investigation deadline.
    v_expires := date_trunc('milliseconds', clock_timestamp()) + interval {ttl};
    IF p_action = 'PROTECTED_RETRY' THEN
        v_deadline := {s}.safeact_investigation_deadline(p_operation);
        IF v_deadline IS NOT NULL AND v_deadline < v_expires THEN
            v_expires := v_deadline;
        END IF;
        IF v_expires <= clock_timestamp() THEN
            RAISE EXCEPTION 'Not a permitted next action: deadline_passed'
                USING ERRCODE = {not_eligible};
        END IF;
    END IF;

    SELECT * INTO v_bind FROM {s}.safeact_proposal_digest(
        p_operation, p_action, p_reference, v_expires
    );

    INSERT INTO {s}.safeact_proposals (
        proposal_id, operation_id, tenant_id, action, rationale,
        attested_reference, proposed_by, bound_seq, bound_state,
        bound_payload_sha256, bound_evidence_digest, bound_connector_id,
        bound_contract_version, binding_digest, expires_at
    ) VALUES (
        v_id, p_operation, v.tenant_id, p_action, btrim(p_rationale),
        p_reference, v.principal_id, v_bind.seq, v_bind.state,
        v_bind.payload_sha256, v_bind.evidence_digest, v_bind.connector_id,
        v_bind.contract_version, v_bind.digest, v_expires
    );

    INSERT INTO {s}.safeact_audit_events (
        operation_id, event_type, actor_id, details
    ) VALUES (
        p_operation, 'PROPOSAL_RECORDED', v.principal_id::text,
        jsonb_build_object(
            'schema_version', 1, 'proposal_id', v_id, 'action', p_action,
            'bound_seq', v_bind.seq, 'binding_digest', v_bind.digest
        )
    );

    RETURN {s}.safeact_proposal_view(v_id);
END;
""")


def _proposals(s):
    return _function(
        s, "safeact_api_proposals",
        "p_secret text, p_status text, p_limit integer",
        "TABLE (proposal jsonb)", """
#variable_conflict use_column
DECLARE
    v record;
BEGIN
    SELECT * INTO v
      FROM {s}.safeact_api_session(p_secret, NULL, false, NULL);
    IF NOT (v.roles && ARRAY['investigator', 'reviewer']::text[]) THEN
        RAISE EXCEPTION 'Role required' USING ERRCODE = {role_required};
    END IF;
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND {max_page}
       OR p_status IS NULL
       OR p_status NOT IN ('all', 'open', 'stale', 'expired',
                           'approved', 'rejected') THEN
        RAISE EXCEPTION 'Invalid request' USING ERRCODE = {invalid};
    END IF;

    RETURN QUERY
    SELECT x.item FROM (
        SELECT {s}.safeact_proposal_view(p.proposal_id) AS item,
               p.created_at, p.proposal_id
          FROM {s}.safeact_proposals p
         WHERE p.tenant_id = v.tenant_id
    ) x
    WHERE p_status = 'all' OR x.item ->> 'status' = p_status
    ORDER BY x.created_at DESC, x.proposal_id DESC
    LIMIT p_limit;
END;
""")


def _proposal(s):
    return _function(
        s, "safeact_api_proposal", "p_secret text, p_proposal uuid",
        "jsonb", """
DECLARE
    v record;
BEGIN
    SELECT * INTO v
      FROM {s}.safeact_api_session(p_secret, NULL, false, NULL);
    IF NOT (v.roles && ARRAY['investigator', 'reviewer']::text[])
       OR NOT EXISTS (
           SELECT 1 FROM {s}.safeact_proposals p
           WHERE p.proposal_id = p_proposal AND p.tenant_id = v.tenant_id
       ) THEN
        RAISE EXCEPTION 'Unknown proposal' USING ERRCODE = {unknown};
    END IF;
    RETURN {s}.safeact_proposal_view(p_proposal);
END;
""")


def _decide(s):
    return _function(
        s, "safeact_api_decide",
        "p_secret text, p_csrf text, p_proposal uuid, p_decision text, "
        "p_client_digest text, p_decision_key uuid, p_note text",
        "jsonb", """
DECLARE
    v record;
    v_p {s}.safeact_proposals%ROWTYPE;
    v_d {s}.safeact_decisions%ROWTYPE;
    v_lock record;
    v_now record;
    v_stale text;
    v_attempt uuid;
BEGIN
    SELECT * INTO v
      FROM {s}.safeact_api_session(p_secret, p_csrf, true, 'reviewer');

    -- Only a person decides: sessions exist only for humans signed in
    -- with a password, and a service principal is never a reviewer.
    IF v.kind IS DISTINCT FROM 'human' THEN
        RAISE EXCEPTION 'Role required' USING ERRCODE = {role_required};
    END IF;
    IF p_decision IS NULL OR p_decision NOT IN ('approve', 'reject')
       OR p_decision_key IS NULL
       OR (p_note IS NOT NULL AND length(p_note) > {max_rationale}) THEN
        RAISE EXCEPTION 'Invalid request' USING ERRCODE = {invalid};
    END IF;

    SELECT * INTO v_p FROM {s}.safeact_proposals p
     WHERE p.proposal_id = p_proposal AND p.tenant_id = v.tenant_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Unknown proposal' USING ERRCODE = {unknown};
    END IF;

    IF v_p.proposed_by = v.principal_id
       OR {s}.safeact_api_requested_by(v_p.operation_id)
          = v.principal_id::text THEN
        RAISE EXCEPTION 'A reviewer cannot decide on their own request or proposal'
            USING ERRCODE = {self_review};
    END IF;

    -- One decision at a time per operation: two reviewers serialize here.
    SELECT * INTO v_lock FROM {s}.safeact_lifecycle_lock(v_p.operation_id);

    SELECT * INTO v_d FROM {s}.safeact_decisions d
     WHERE d.proposal_id = p_proposal;
    IF FOUND THEN
        IF v_d.decision_key = p_decision_key AND v_d.decided_by = v.principal_id
           AND v_d.decision = p_decision THEN
            RETURN jsonb_build_object(
                'outcome', 'replayed', 'reason', NULL,
                'proposal', {s}.safeact_proposal_view(p_proposal)
            );
        END IF;
        RAISE EXCEPTION 'Proposal already decided' USING ERRCODE = {decided};
    END IF;

    IF p_decision = 'approve' THEN
        SELECT * INTO v_now FROM {s}.safeact_proposal_digest(
            v_p.operation_id, v_p.action, v_p.attested_reference,
            v_p.expires_at
        );
        v_stale := CASE
            WHEN v_p.expires_at <= clock_timestamp() THEN 'expired'
            WHEN v_now.seq IS DISTINCT FROM v_p.bound_seq
                 OR v_now.state IS DISTINCT FROM v_p.bound_state THEN
                'operation_changed'
            WHEN v_now.evidence_digest IS DISTINCT FROM v_p.bound_evidence_digest
                THEN 'evidence_changed'
            WHEN v_now.digest IS DISTINCT FROM v_p.binding_digest THEN
                'binding_changed'
            WHEN p_client_digest IS DISTINCT FROM v_p.binding_digest THEN
                'client_out_of_date'
            ELSE {s}.safeact_proposal_eligibility(
                v_p.operation_id, v_p.action, v_p.attested_reference
            ) END;

        IF v_stale IS NOT NULL THEN
            -- Kept: the refusal is part of the audit trail. Nothing else
            -- changes, and the proposal stays undecided.
            INSERT INTO {s}.safeact_audit_events (
                operation_id, event_type, actor_id, details
            ) VALUES (
                v_p.operation_id, 'APPROVAL_REFUSED_STALE',
                v.principal_id::text,
                jsonb_build_object(
                    'schema_version', 1, 'proposal_id', p_proposal,
                    'reason', v_stale, 'bound_seq', v_p.bound_seq,
                    'current_seq', v_now.seq
                )
            );
            RETURN jsonb_build_object(
                'outcome', 'refused_stale', 'reason', v_stale,
                'proposal', {s}.safeact_proposal_view(p_proposal)
            );
        END IF;
    END IF;

    INSERT INTO {s}.safeact_decisions (
        proposal_id, decision, decided_by, decision_key, note
    ) VALUES (
        p_proposal, p_decision, v.principal_id, p_decision_key, p_note
    );

    IF p_decision = 'approve' AND v_p.action = 'PROTECTED_RETRY' THEN
        -- Through the guarded append: retry basis, attempt budget and
        -- this approval are all checked again there.
        PERFORM {s}.safeact_lifecycle_append(
            v_p.operation_id, v_lock.o_seq + 1, 'VERIFYING', 'RETRY_ELIGIBLE',
            v_lock.o_claim, v_lock.o_attempt, NULL,
            'retry_approved_by_reviewer',
            jsonb_build_object('proposal_id', p_proposal,
                               'approved_by', v.principal_id),
            'LIFECYCLE_RETRY_APPROVED'
        );
    ELSIF p_decision = 'approve' AND v_p.action = 'ATTEST_APPLIED' THEN
        SELECT a.attempt_id INTO v_attempt
          FROM {s}.safeact_attempts a
         WHERE a.operation_id = v_p.operation_id
         ORDER BY a.ordinal DESC LIMIT 1;
        INSERT INTO {s}.safeact_attempt_outcomes (
            outcome_id, operation_id, attempt_id, outcome, evidence_kind,
            evidence_reference, observed_by, observed_seq, details,
            provenance
        ) VALUES (
            gen_random_uuid(), v_p.operation_id, v_attempt,
            'confirmed_applied', 'applied', v_p.attested_reference,
            'reviewer:' || v.username, v_lock.o_seq,
            jsonb_build_object(
                'source', 'human_attestation',
                'proposal_id', p_proposal,
                'proposed_by', v_p.proposed_by,
                'approved_by', v.principal_id
            ),
            'human_attested'
        );
        INSERT INTO {s}.safeact_audit_events (
            operation_id, event_type, actor_id, details
        ) VALUES (
            v_p.operation_id, 'ATTEMPT_EVIDENCE_RECORDED',
            v.principal_id::text,
            jsonb_build_object(
                'schema_version', 1, 'attempt_id', v_attempt,
                'outcome', 'confirmed_applied', 'provenance', 'human_attested',
                'observed_seq', v_lock.o_seq, 'proposal_id', p_proposal
            )
        );
    END IF;

    INSERT INTO {s}.safeact_audit_events (
        operation_id, event_type, actor_id, details
    ) VALUES (
        v_p.operation_id, 'DECISION_RECORDED', v.principal_id::text,
        jsonb_build_object(
            'schema_version', 1, 'proposal_id', p_proposal,
            'decision', p_decision, 'action', v_p.action,
            'bound_seq', v_p.bound_seq
        )
    );

    RETURN jsonb_build_object(
        'outcome', CASE WHEN p_decision = 'approve' THEN 'approved'
                        ELSE 'rejected' END,
        'reason', NULL,
        'proposal', {s}.safeact_proposal_view(p_proposal)
    );
END;
""")


def _retry_status(s):
    return _function(s, "safeact_retry_approval_status", "p_operation uuid",
                     "text", """
DECLARE
    v_state text;
    v_seq integer;
    v_policy boolean;
    v_basis text;
BEGIN
    SELECT c.state, c.seq INTO v_state, v_seq
      FROM {s}.{view} c WHERE c.operation_id = p_operation;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Unknown operation' USING ERRCODE = {unknown};
    END IF;
    SELECT coalesce(r.retry_requires_approval, false) INTO v_policy
      FROM {s}.safeact_operations o
      LEFT JOIN {s}.safeact_contract_review_policies r
        ON r.connector_id = o.connector_id AND r.version = o.contract_version
     WHERE o.operation_id = p_operation;

    IF NOT v_policy THEN
        RETURN 'not_required';
    ELSIF v_state IS DISTINCT FROM 'VERIFYING' THEN
        RETURN 'not_applicable';
    END IF;
    v_basis := {s}.safeact_retry_basis(p_operation);
    IF (v_basis IS DISTINCT FROM 'proof' AND v_basis IS DISTINCT FROM 'same_key')
       OR (SELECT count(*) FROM {s}.safeact_attempts a
            WHERE a.operation_id = p_operation)
          >= {s}.safeact_attempt_budget(p_operation) THEN
        RETURN 'not_eligible';
    ELSIF {s}.safeact_retry_approved(p_operation, v_seq) THEN
        RETURN 'approved';
    END IF;
    RETURN 'awaiting';
END;
""", volatility="STABLE")


def _take_reverify(s):
    return _function(s, "safeact_take_reverify", "p_operation uuid",
                     "uuid", """
DECLARE
    v_lock record;
    v_proposal uuid;
BEGIN
    SELECT * INTO v_lock FROM {s}.safeact_lifecycle_lock(p_operation);
    IF v_lock.o_state IS DISTINCT FROM 'ESCALATED_UNRESOLVED' THEN
        RETURN NULL;
    END IF;

    SELECT p.proposal_id INTO v_proposal
      FROM {s}.safeact_proposals p
      JOIN {s}.safeact_decisions d ON d.proposal_id = p.proposal_id
     WHERE p.operation_id = p_operation
       AND p.action = 'REVERIFY'
       AND p.bound_seq = v_lock.o_seq
       AND d.decision = 'approve'
       AND NOT EXISTS (
           SELECT 1 FROM {s}.safeact_reverify_uses u
           WHERE u.proposal_id = p.proposal_id
       )
     ORDER BY d.created_at
     LIMIT 1;

    IF v_proposal IS NOT NULL THEN
        INSERT INTO {s}.safeact_reverify_uses (proposal_id)
        VALUES (v_proposal);
    END IF;
    RETURN v_proposal;
END;
""")


def _succeeded_by_provenance(s):
    return _function(
        s, "safeact_succeeded_by_provenance", "",
        "TABLE (provenance text, operations bigint)", """
#variable_conflict use_column
BEGIN
    -- A success counts as provider-verified when any system-recorded
    -- applied evidence exists; otherwise it rests on a human attestation.
    RETURN QUERY
    SELECT CASE WHEN EXISTS (
               SELECT 1 FROM {s}.safeact_attempt_outcomes x
               WHERE x.operation_id = c.operation_id
                 AND x.outcome = 'confirmed_applied'
                 AND x.evidence_kind = 'applied'
                 AND x.provenance = 'system'
           ) THEN 'provider_verified' ELSE 'human_attested' END,
           count(*)
      FROM {s}.{view} c
     WHERE c.state = 'SUCCEEDED'
     GROUP BY 1;
END;
""", volatility="STABLE")


def _signature(s, name, arguments):
    return sql.SQL("{}.{}({})").format(
        s, sql.Identifier(name), sql.SQL(arguments)
    )


_BUILDERS = (
    _evidence_digest, _retry_approved, _eligibility, _digest, _view_fn,
    _propose, _proposals, _proposal, _decide, _retry_status,
    _take_reverify, _succeeded_by_provenance,
)


def upgrade_statements(schema):
    s = v0003._schema(schema)
    return _tables(s) + [
        build(s) for build in _BUILDERS
    ] + [_append(s), _operation(s)] + [
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
    outcomes = sql.SQL("{}.safeact_attempt_outcomes").format(s)
    previous = v0012._operation(s).as_string(None)
    return [
        v0012._append(s),
        sql.SQL("CREATE OR REPLACE" + previous[len("CREATE"):]),
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
    ] + [
        sql.SQL(
            "ALTER TABLE {} DROP CONSTRAINT IF EXISTS "
            "ck_safeact_attempt_outcomes_provenance"
        ).format(outcomes),
        sql.SQL(
            "ALTER TABLE {} DROP COLUMN IF EXISTS provenance"
        ).format(outcomes),
    ]
