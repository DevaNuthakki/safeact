"""FROZEN once committed: SQL for revision 0016_agent_keys.

Agent keys and the MCP boundary (Milestone 4).

- Service principals authenticate with administrator-issued keys
  ('sak_' + 64 hex, 32 random bytes). Only the SHA-256 is stored. A key
  carries scopes from {submit, investigate}; reviewer cannot be
  expressed (CHECK), and the identity rule still refuses any reviewer
  grant for a non-human. Keys expire within 30 days and can be revoked
  (append-only).
- Identity is derived inside PostgreSQL on every tool call. Each
  safeact_mcp_* function (the only functions the MCP role may execute)
  resolves the key, mints a one-minute session that never leaves the
  transaction, links it to the key and the scope the tool needs, and
  calls the existing safeact_api_* function with it. All product rules
  (grants, visibility, eligibility, binding) are reused unchanged and
  the audit actor is the service principal.
- safeact_api_session (replaced) re-checks the key for a key-linked
  session and narrows its roles to the scope (submit -> requester,
  investigate -> investigator). A service principal's session that is
  not key-linked is refused.
- safeact_api_visible (replaced): a service principal sees only the
  operations it requested, whatever its roles.
- Agent-only admission (blueprint F15, "agent changes key"): an agent
  never chooses a request key; it is derived from (principal, target,
  intent reference). A second operation by the same principal on the
  same granted target is refused (SAT28) unless every earlier one ended
  FAILED_CONFIRMED or CANCELLED_BEFORE_CLAIM. Known limit: an agent can
  never perform a second distinct action on the same target (for
  example two partial refunds); that needs the human path.
- An agent may propose REVERIFY or PROTECTED_RETRY only, never
  ATTEST_APPLIED, and has no decision function at all.

Pinned by tests/phase_a/test_migration_sql_frozen.py. Values are
literals; nothing is read from core.
"""

from psycopg import sql

from storage.lifecycle_sql import v0003, v0011, v0012, v0013


REVISION = "0016_agent_keys"

CONFLICTING_OPERATION = "SAT28"

KEY_PATTERN = "^sak_[0-9a-f]{64}$"
SCOPES = ("investigate", "submit")
SCOPE_ROLES = {"investigate": "investigator", "submit": "requester"}
KEY_MAX_LIFETIME = "30 days"
CALL_SESSION_LIFETIME = "1 minute"
REVOCATION_REASONS = ("compromised", "retired", "rotated")
TOOLS = ("operation", "propose", "request_verification", "submit")
AGENT_PROPOSALS = ("PROTECTED_RETRY", "REVERIFY")
SETTLED_STATES = ("CANCELLED_BEFORE_CLAIM", "FAILED_CONFIRMED")
INTENT_PATTERN = "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$"

NEW_TABLES = (
    "safeact_agent_keys",
    "safeact_agent_key_revocations",
    "safeact_agent_call_sessions",
)
HISTORY_TABLES = tuple(reversed(NEW_TABLES))
NEW_INDEXES = ("ix_safeact_agent_keys_principal",)
LEGAL_TRANSITIONS = frozenset()

NEW_FUNCTIONS = {
    "safeact_agent_key_guard": "",
    "safeact_agent_call": "text, text, text",
    "safeact_mcp_submit": "text, text, text, text, text, text, text, text",
    "safeact_mcp_operation": "text, uuid",
    "safeact_mcp_request_verification": "text, uuid",
    "safeact_mcp_propose": "text, uuid, text, text",
}
REPLACED_FUNCTIONS = (
    "safeact_api_session",
    "safeact_api_visible",
)

SHARED_FUNCTIONS = {}
WORKER_FUNCTIONS = {}
RECOVERY_FUNCTIONS = {}
ADVANCER_FUNCTIONS = {}
API_FUNCTIONS = {}
INTERNAL_FUNCTIONS = {
    "safeact_agent_key_guard": "",
    "safeact_agent_call": "text, text, text",
}
MCP_FUNCTIONS = {
    name: arguments for name, arguments in NEW_FUNCTIONS.items()
    if name not in INTERNAL_FUNCTIONS
}
OWNER_TABLE_PRIVILEGES = {
    "safeact_agent_keys": ("SELECT",),
    "safeact_agent_key_revocations": ("SELECT",),
    "safeact_agent_call_sessions": ("SELECT", "INSERT"),
}

_HEADER = (
    "LANGUAGE plpgsql {security} "
    "SET search_path = pg_catalog, pg_temp "
    "AS $safeact_lifecycle$ "
)


def _array(values):
    return sql.SQL("ARRAY[{}]::text[]").format(
        sql.SQL(", ").join(sql.Literal(v) for v in values)
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
    return sql.SQL(header + body + " $safeact_lifecycle$").format(**_values(
        s, name=sql.Identifier(name)
    ))


def _values(s, **extra):
    return dict(
        s=s,
        unauthenticated=sql.Literal(v0011.UNAUTHENTICATED),
        role_required=sql.Literal(v0011.ROLE_REQUIRED),
        identity_invalid=sql.Literal(v0011.IDENTITY_INVALID),
        invalid=sql.Literal(v0012.REQUEST_INVALID),
        not_eligible=sql.Literal(v0013.NOT_ELIGIBLE),
        conflicting=sql.Literal(CONFLICTING_OPERATION),
        key_pattern=sql.Literal(KEY_PATTERN),
        intent_pattern=sql.Literal(INTENT_PATTERN),
        scopes=_array(SCOPES),
        tools=_array(TOOLS),
        reasons=_array(REVOCATION_REASONS),
        proposals=_array(AGENT_PROPOSALS),
        settled=_array(SETTLED_STATES),
        key_lifetime=sql.Literal(KEY_MAX_LIFETIME),
        call_lifetime=sql.Literal(CALL_SESSION_LIFETIME),
        view=sql.Identifier(v0003.CURRENT_STATE_VIEW),
        **extra,
    )


def _tables(s):
    v = _values(s)
    return [
        sql.SQL("""
CREATE TABLE {s}.safeact_agent_keys (
    key_id uuid PRIMARY KEY,
    principal_id uuid NOT NULL
        REFERENCES {s}.safeact_principals (principal_id)
        ON DELETE RESTRICT,
    key_sha256 bytea NOT NULL UNIQUE,
    scopes text[] NOT NULL,
    label varchar(64) NOT NULL,
    created_by varchar(128) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    CONSTRAINT ck_safeact_agent_keys_hash
        CHECK (octet_length(key_sha256) = 32),
    CONSTRAINT ck_safeact_agent_keys_scopes CHECK (
        cardinality(scopes) > 0
        AND array_position(scopes, NULL) IS NULL
        AND scopes <@ {scopes}
    ),
    CONSTRAINT ck_safeact_agent_keys_label CHECK (
        label ~ '^[a-z0-9][a-z0-9._-]{{0,63}}$'
    ),
    CONSTRAINT ck_safeact_agent_keys_created_by CHECK (
        length(created_by) > 0 AND created_by = btrim(created_by)
        AND created_by !~ '[[:cntrl:]]'
    ),
    CONSTRAINT ck_safeact_agent_keys_lifetime CHECK (
        expires_at > created_at
        AND expires_at <= created_at + interval {key_lifetime}
    )
)""").format(**v),
        sql.SQL(
            "CREATE INDEX ix_safeact_agent_keys_principal "
            "ON {s}.safeact_agent_keys (principal_id)"
        ).format(**v),
        sql.SQL("""
CREATE TABLE {s}.safeact_agent_key_revocations (
    key_id uuid PRIMARY KEY
        REFERENCES {s}.safeact_agent_keys (key_id)
        ON DELETE RESTRICT,
    reason varchar(32) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_safeact_agent_key_revocations_reason
        CHECK (reason = ANY ({reasons}))
)""").format(**v),
        sql.SQL("""
CREATE TABLE {s}.safeact_agent_call_sessions (
    session_id uuid PRIMARY KEY
        REFERENCES {s}.safeact_sessions (session_id)
        ON DELETE RESTRICT,
    key_id uuid NOT NULL
        REFERENCES {s}.safeact_agent_keys (key_id)
        ON DELETE RESTRICT,
    tool varchar(32) NOT NULL,
    scopes text[] NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_safeact_agent_call_sessions_tool
        CHECK (tool = ANY ({tools})),
    CONSTRAINT ck_safeact_agent_call_sessions_scopes CHECK (
        cardinality(scopes) > 0
        AND array_position(scopes, NULL) IS NULL
        AND scopes <@ {scopes}
    )
)""").format(**v),
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


def _key_guard(s):
    # A trigger, not a CHECK: the principal's kind lives in another
    # table. Only a service principal holds an agent key.
    return _function(s, "safeact_agent_key_guard", "", "trigger", """
DECLARE
    v_kind text;
BEGIN
    EXECUTE format(
        'SELECT kind FROM %I.safeact_principals WHERE principal_id = $1',
        TG_TABLE_SCHEMA
    ) INTO v_kind USING NEW.principal_id;
    IF v_kind IS DISTINCT FROM 'service' THEN
        RAISE EXCEPTION 'Only a service principal can hold an agent key'
            USING ERRCODE = {identity_invalid};
    END IF;
    RETURN NEW;
END;
""", definer=False)


def _guard_trigger(s):
    return sql.SQL(
        "CREATE TRIGGER safeact_agent_keys_identity_guard BEFORE INSERT "
        "ON {s}.safeact_agent_keys FOR EACH ROW "
        "EXECUTE FUNCTION {s}.safeact_agent_key_guard()"
    ).format(s=s)


def _call(s):
    # The one place an agent key is resolved. Raises the same
    # 'Not authenticated' for every key problem; a missing scope is
    # 'Role required', as for a person.
    return _function(
        s, "safeact_agent_call", "p_key text, p_scope text, p_tool text",
        "TABLE (session_secret text, csrf_token text, principal_id uuid, "
        "tenant_id text)", """
DECLARE
    v_key {s}.safeact_agent_keys%ROWTYPE;
    v_principal {s}.safeact_principals%ROWTYPE;
    v_scopes text[];
    v_session uuid := gen_random_uuid();
    v_secret text;
    v_csrf text;
BEGIN
    IF p_key IS NULL OR p_key !~ {key_pattern} THEN
        RAISE EXCEPTION 'Not authenticated'
            USING ERRCODE = {unauthenticated};
    END IF;

    SELECT * INTO v_key FROM {s}.safeact_agent_keys AS k
     WHERE k.key_sha256 = sha256(convert_to(p_key, 'UTF8'));
    IF FOUND THEN
        SELECT * INTO v_principal FROM {s}.safeact_principals AS p
         WHERE p.principal_id = v_key.principal_id;
    END IF;

    IF v_key.key_id IS NULL
       OR v_key.expires_at <= now()
       OR EXISTS (
           SELECT 1 FROM {s}.safeact_agent_key_revocations AS r
            WHERE r.key_id = v_key.key_id
       )
       OR v_principal.kind IS DISTINCT FROM 'service'
       OR EXISTS (
           SELECT 1 FROM {s}.safeact_principal_disablements AS d
            WHERE d.principal_id = v_key.principal_id
       )
    THEN
        RAISE EXCEPTION 'Not authenticated'
            USING ERRCODE = {unauthenticated};
    END IF;

    IF p_scope IS NULL THEN
        v_scopes := v_key.scopes;
    ELSIF p_scope = ANY (v_key.scopes) THEN
        v_scopes := ARRAY[p_scope];
    ELSE
        RAISE EXCEPTION 'Role required' USING ERRCODE = {role_required};
    END IF;

    v_secret := encode(public.gen_random_bytes(32), 'hex');
    v_csrf := encode(public.gen_random_bytes(32), 'hex');
    INSERT INTO {s}.safeact_sessions (
        session_id, principal_id, secret_sha256, csrf_sha256, expires_at
    ) VALUES (
        v_session, v_key.principal_id,
        sha256(convert_to(v_secret, 'UTF8')),
        sha256(convert_to(v_csrf, 'UTF8')),
        now() + interval {call_lifetime}
    );
    INSERT INTO {s}.safeact_agent_call_sessions (
        session_id, key_id, tool, scopes
    ) VALUES (v_session, v_key.key_id, p_tool, v_scopes);

    RETURN QUERY SELECT v_secret, v_csrf, v_key.principal_id,
                        v_principal.tenant_id::text;
END;
""", definer=False)


def _session(s):
    # The frozen 0011 session check, plus: a key-linked session is
    # re-checked against its key and narrowed to its scope; a service
    # principal never has any other kind of session.
    base = v0011._session(s).as_string(None)
    marker = "    IF p_role IS NOT NULL AND NOT (p_role = ANY (v_roles)) THEN"
    if base.count(marker) != 1:
        raise RuntimeError("0011 session no longer has one role check")
    declare = "    v_roles text[];\n"
    if base.count(declare) != 1:
        raise RuntimeError("0011 session declarations changed shape")
    head, tail = base.split(marker)
    head = head.replace(declare, declare + "    v_call record;\n")
    narrow = sql.SQL("""    -- 0016: agent keys. A key-linked session is re-checked against
    -- its key and narrowed to the scope its tool asked for.
    SELECT c.scopes, k.expires_at,
           EXISTS (SELECT 1 FROM {s}.safeact_agent_key_revocations AS r
                    WHERE r.key_id = c.key_id) AS revoked
      INTO v_call
      FROM {s}.safeact_agent_call_sessions AS c
      JOIN {s}.safeact_agent_keys AS k ON k.key_id = c.key_id
     WHERE c.session_id = v_session.session_id;
    IF FOUND THEN
        IF v_call.revoked OR v_call.expires_at <= now()
           OR v_principal.kind IS DISTINCT FROM 'service' THEN
            RAISE EXCEPTION 'Not authenticated'
                USING ERRCODE = {unauthenticated};
        END IF;
        SELECT coalesce(array_agg(r ORDER BY r), '{{}}') INTO v_roles
          FROM unnest(v_roles) AS r
         WHERE (r = 'requester' AND 'submit' = ANY (v_call.scopes))
            OR (r = 'investigator' AND 'investigate' = ANY (v_call.scopes));
    ELSIF v_principal.kind IS DISTINCT FROM 'human' THEN
        RAISE EXCEPTION 'Not authenticated'
            USING ERRCODE = {unauthenticated};
    END IF;

""").format(**_values(s)).as_string(None)
    text = head + narrow + marker + tail
    return sql.SQL("CREATE OR REPLACE" + text[len("CREATE"):])


def _visible(s):
    # The frozen 0012 rule, except that a service principal sees only
    # the operations it requested, whatever its roles.
    return sql.SQL("CREATE OR REPLACE" + _function(
        s, "safeact_api_visible",
        "p_operation uuid, p_principal uuid, p_tenant text, p_roles text[]",
        "boolean", """
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM {s}.safeact_operations o
        WHERE o.operation_id = p_operation AND o.tenant_id = p_tenant
    ) AND (
        (
            p_roles && ARRAY['investigator', 'reviewer']::text[]
            AND EXISTS (
                SELECT 1 FROM {s}.safeact_principals p
                WHERE p.principal_id = p_principal AND p.kind = 'human'
            )
        )
        OR {s}.safeact_api_requested_by(p_operation) = p_principal::text
    );
END;
""", definer=False, volatility="STABLE").as_string(None)[len("CREATE"):])


def _submit(s):
    return _function(
        s, "safeact_mcp_submit",
        "p_key text, p_intent_ref text, p_action text, p_resource text, "
        "p_account text, p_connector text, p_version text, p_payload text",
        "TABLE (admitted_operation uuid, created boolean, request_key text)",
        """
#variable_conflict use_column
DECLARE
    v record;
    v_request_key text;
BEGIN
    SELECT * INTO v FROM {s}.safeact_agent_call(p_key, 'submit', 'submit');

    IF p_intent_ref IS NULL OR p_intent_ref !~ {intent_pattern}
       OR p_action IS NULL OR p_resource IS NULL OR p_account IS NULL
       OR p_connector IS NULL OR p_version IS NULL THEN
        RAISE EXCEPTION 'Invalid request' USING ERRCODE = {invalid};
    END IF;

    -- The agent never chooses a request key: the same intent on the
    -- same target is the same operation.
    v_request_key := 'ag:' || encode(sha256(convert_to(
        concat_ws(chr(31), v.principal_id::text, p_connector, p_account,
                  p_action, p_resource, p_intent_ref),
        'UTF8')), 'hex');

    -- One open (or succeeded) operation per principal and target.
    PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws(
        chr(31), 'safeact_mcp_submit', v.tenant_id, p_connector, p_account,
        p_action, p_resource), 0));
    IF EXISTS (
        SELECT 1
          FROM {s}.safeact_operations o
          JOIN {s}.{view} c ON c.operation_id = o.operation_id
         WHERE o.tenant_id = v.tenant_id
           AND o.connector_id = p_connector
           AND o.provider_account_id = p_account
           AND o.action_type = p_action
           AND o.resource_id = p_resource
           AND o.request_key <> v_request_key
           AND NOT (c.state = ANY ({settled}))
           AND {s}.safeact_api_requested_by(o.operation_id)
               = v.principal_id::text
    ) THEN
        RAISE EXCEPTION 'Another operation on this target is not settled'
            USING ERRCODE = {conflicting};
    END IF;

    RETURN QUERY SELECT x.admitted_operation, x.created, v_request_key
      FROM {s}.safeact_api_submit(
          v.session_secret, v.csrf_token, v_request_key, p_action,
          p_resource, p_account, p_connector, p_version, p_payload
      ) AS x;
END;
""")


def _operation(s):
    return _function(
        s, "safeact_mcp_operation", "p_key text, p_operation uuid", "jsonb",
        """
DECLARE
    v record;
BEGIN
    SELECT * INTO v FROM {s}.safeact_agent_call(p_key, NULL, 'operation');
    RETURN {s}.safeact_api_operation(v.session_secret, p_operation);
END;
""")


def _request_verification(s):
    return _function(
        s, "safeact_mcp_request_verification",
        "p_key text, p_operation uuid", "jsonb", """
DECLARE
    v record;
BEGIN
    SELECT * INTO v FROM {s}.safeact_agent_call(
        p_key, 'investigate', 'request_verification'
    );
    RETURN {s}.safeact_api_request_verification(
        v.session_secret, v.csrf_token, p_operation
    );
END;
""")


def _propose(s):
    return _function(
        s, "safeact_mcp_propose",
        "p_key text, p_operation uuid, p_action text, p_rationale text",
        "jsonb", """
DECLARE
    v record;
BEGIN
    SELECT * INTO v FROM {s}.safeact_agent_call(
        p_key, 'investigate', 'propose'
    );
    -- An agent never originates attested evidence.
    IF p_action IS NULL OR NOT (p_action = ANY ({proposals})) THEN
        RAISE EXCEPTION 'Not a permitted proposal for an agent'
            USING ERRCODE = {not_eligible};
    END IF;
    RETURN {s}.safeact_api_propose(
        v.session_secret, v.csrf_token, p_operation, p_action, p_rationale,
        NULL
    );
END;
""")


def _signature(s, name, arguments):
    return sql.SQL("{}.{}({})").format(
        s, sql.Identifier(name), sql.SQL(arguments)
    )


def upgrade_statements(schema):
    s = v0003._schema(schema)
    return _tables(s) + [
        _key_guard(s), _guard_trigger(s), _call(s), _session(s),
        _visible(s), _submit(s), _operation(s), _request_verification(s),
        _propose(s),
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


def _restore(statement):
    text = statement.as_string(None)
    return sql.SQL("CREATE OR REPLACE" + text[len("CREATE"):])


def downgrade_statements(schema):
    s = v0003._schema(schema)
    return [
        _restore(v0011._session(s)),
        _restore(v0012._visible(s)),
    ] + [
        sql.SQL("DROP FUNCTION IF EXISTS {}").format(
            _signature(s, name, arguments)
        )
        for name, arguments in reversed(tuple(NEW_FUNCTIONS.items()))
        if name != "safeact_agent_key_guard"
    ] + [
        sql.SQL("DROP TABLE IF EXISTS {}.{}").format(
            s, sql.Identifier(table)
        )
        for table in reversed(NEW_TABLES)
    ] + [
        sql.SQL("DROP FUNCTION IF EXISTS {}").format(
            _signature(s, "safeact_agent_key_guard", "")
        ),
    ]
