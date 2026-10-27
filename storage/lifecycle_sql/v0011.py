"""FROZEN once committed: SQL for revision 0011_identity_sessions.

Human identity and sessions verified inside PostgreSQL (Milestone 3).

- safeact_principals: a person or service in exactly one tenant. Kind
  is fixed ('human' or 'service'). Demo principals (username prefixed
  'demo-') are refused outside the safeact_demo and safeact_it_*
  schemas by a trigger that reads the table's own schema.
- safeact_credentials: one bcrypt hash (cost 12, pgcrypto) per human.
  A service principal can never hold a password.
- safeact_role_grants: requester, investigator, reviewer. Only a human
  can be a reviewer.
- safeact_action_grants: exact-scope action grants, the persisted form
  of core.admission_authorization.AdmissionGrant (tenant comes from
  the principal). Used by the admission migration.
- safeact_sessions stores only SHA-256 hashes of the session secret and
  the CSRF token. The plaintext of each is returned once, by login.
  Absolute expiry 8 h; idle expiry 30 min, tracked by append-only
  activity rows written at most once a minute; logout appends a
  revocation. No identity row is ever updated or deleted.
- safeact_api_login never raises on bad input, a bad password, an
  unknown user or a locked account: it records the attempt and returns
  ok = false, so the caller's commit keeps the failure row and the
  throttle can trigger. Every path runs exactly one bcrypt check (a
  fixed dummy hash for unknown users) so timing does not reveal
  whether an account exists. Five credential failures in 15 minutes
  lock the account; twenty failures from one source lock the source.
- safeact_api_session (internal, SECURITY INVOKER: it only ever runs
  as the owner, inside the definer functions) derives the principal,
  tenant and roles from the session secret alone, checks expiry, revocation and,
  for writes, the CSRF token; every product function calls it. The API
  role may execute login, logout and whoami only.

Secrets are only ever compared as hashes and never appear in a raised
message. Pinned by tests/phase_a/test_migration_sql_frozen.py. Values
are literals; nothing is read from core.
"""

from psycopg import sql

from storage.lifecycle_sql import v0003


REVISION = "0011_identity_sessions"

UNAUTHENTICATED = "SAT17"
CSRF_REFUSED = "SAT18"
ROLE_REQUIRED = "SAT19"
IDENTITY_INVALID = "SAT20"

BCRYPT_COST = 12
# bcrypt of a random 32-byte value; its plaintext was discarded.
DUMMY_HASH = "$2a$12$KcFaeR5EGzlHLwfMsVbZGOiDrFiTDc1550pkaB31raO0Mhg8WizEq"
SESSION_ABSOLUTE = "8 hours"
SESSION_IDLE = "30 minutes"
TOUCH_EVERY = "60 seconds"
LOGIN_WINDOW = "15 minutes"
ACCOUNT_FAILURES = 5
SOURCE_FAILURES = 20
MAX_PASSWORD_BYTES = 72
ROLES = ("investigator", "requester", "reviewer")

NEW_TABLES = (
    "safeact_principals",
    "safeact_principal_disablements",
    "safeact_credentials",
    "safeact_role_grants",
    "safeact_action_grants",
    "safeact_login_attempts",
    "safeact_sessions",
    "safeact_session_activity",
    "safeact_session_revocations",
)
HISTORY_TABLES = tuple(reversed(NEW_TABLES))
NEW_INDEXES = (
    "ix_safeact_login_attempts_principal",
    "ix_safeact_login_attempts_source",
    "ix_safeact_session_activity_session",
)
LEGAL_TRANSITIONS = frozenset()

NEW_FUNCTIONS = {
    "safeact_identity_guard": "",
    "safeact_password_matches": "text, text",
    "safeact_api_session": "text, text, boolean, text",
    "safeact_api_login": "text, text, text",
    "safeact_api_logout": "text, text",
    "safeact_api_whoami": "text",
}
REPLACED_FUNCTIONS = ()

SHARED_FUNCTIONS = {}
WORKER_FUNCTIONS = {}
RECOVERY_FUNCTIONS = {}
ADVANCER_FUNCTIONS = {}
INTERNAL_FUNCTIONS = {
    "safeact_identity_guard": "",
    "safeact_password_matches": "text, text",
    "safeact_api_session": "text, text, boolean, text",
}
API_FUNCTIONS = {
    "safeact_api_login": "text, text, text",
    "safeact_api_logout": "text, text",
    "safeact_api_whoami": "text",
}
OWNER_TABLE_PRIVILEGES = {
    "safeact_principals": ("SELECT",),
    "safeact_principal_disablements": ("SELECT",),
    "safeact_credentials": ("SELECT",),
    "safeact_role_grants": ("SELECT",),
    "safeact_action_grants": ("SELECT",),
    "safeact_login_attempts": ("SELECT", "INSERT"),
    "safeact_sessions": ("SELECT", "INSERT"),
    "safeact_session_activity": ("SELECT", "INSERT"),
    "safeact_session_revocations": ("SELECT", "INSERT"),
}

_HEADER = (
    "LANGUAGE plpgsql {security} "
    "SET search_path = pg_catalog, pg_temp "
    "AS $safeact_lifecycle$ "
)

# Exact identifiers: no empty, padded, wildcard or control characters
# (same rule as storage.authority_schema).
_EXACT = (
    "length({c}) > 0 AND {c} = btrim({c}) "
    "AND strpos({c}, '*') = 0 AND {c} !~ '[[:cntrl:]]'"
)


def _exact(*columns):
    return " AND ".join(_EXACT.format(c=c) for c in columns)


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
        unauthenticated=sql.Literal(UNAUTHENTICATED),
        csrf_refused=sql.Literal(CSRF_REFUSED),
        role_required=sql.Literal(ROLE_REQUIRED),
        identity_invalid=sql.Literal(IDENTITY_INVALID),
        dummy=sql.Literal(DUMMY_HASH),
        absolute=sql.Literal(SESSION_ABSOLUTE),
        idle=sql.Literal(SESSION_IDLE),
        touch=sql.Literal(TOUCH_EVERY),
        window=sql.Literal(LOGIN_WINDOW),
        account_failures=sql.Literal(ACCOUNT_FAILURES),
        source_failures=sql.Literal(SOURCE_FAILURES),
        max_password=sql.Literal(MAX_PASSWORD_BYTES),
    )


def _tables(s):
    roles = ", ".join(f"'{r}'" for r in ROLES)
    return [
        sql.SQL("""
CREATE TABLE {s}.safeact_principals (
    principal_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    username varchar(64) NOT NULL,
    kind varchar(16) NOT NULL,
    display_name varchar(128) NOT NULL,
    demo boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_safeact_principals_username UNIQUE (username),
    CONSTRAINT uq_safeact_principals_tenant
        UNIQUE (principal_id, tenant_id),
    CONSTRAINT ck_safeact_principals_kind
        CHECK (kind IN ('human', 'service')),
    CONSTRAINT ck_safeact_principals_username
        CHECK (username ~ '^[a-z0-9][a-z0-9._-]{{2,63}}$'),
    CONSTRAINT ck_safeact_principals_demo
        CHECK (demo = (username LIKE 'demo-%')),
    CONSTRAINT ck_safeact_principals_text
        CHECK (""" + _exact("tenant_id", "display_name") + """)
)""").format(s=s),

        sql.SQL("""
CREATE TABLE {s}.safeact_principal_disablements (
    principal_id uuid PRIMARY KEY
        REFERENCES {s}.safeact_principals (principal_id)
        ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now()
)""").format(s=s),

        sql.SQL("""
CREATE TABLE {s}.safeact_credentials (
    principal_id uuid PRIMARY KEY
        REFERENCES {s}.safeact_principals (principal_id)
        ON DELETE RESTRICT,
    password_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_safeact_credentials_bcrypt
        CHECK (password_hash ~ '^[$]2a[$]12[$][./A-Za-z0-9]{{53}}$')
)""").format(s=s),

        sql.SQL("""
CREATE TABLE {s}.safeact_role_grants (
    principal_id uuid NOT NULL
        REFERENCES {s}.safeact_principals (principal_id)
        ON DELETE RESTRICT,
    role varchar(16) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_safeact_role_grants PRIMARY KEY (principal_id, role),
    CONSTRAINT ck_safeact_role_grants_role CHECK (role IN (""" + roles + """))
)""").format(s=s),

        sql.SQL("""
CREATE TABLE {s}.safeact_action_grants (
    principal_id uuid NOT NULL
        REFERENCES {s}.safeact_principals (principal_id)
        ON DELETE RESTRICT,
    connector_id varchar(64) NOT NULL,
    provider_account_id varchar(128) NOT NULL,
    action_type varchar(64) NOT NULL,
    resource_id varchar(255) NOT NULL,
    contract_version varchar(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_safeact_action_grants PRIMARY KEY (
        principal_id, connector_id, provider_account_id, action_type,
        resource_id, contract_version
    ),
    CONSTRAINT ck_safeact_action_grants_scope CHECK (""" + _exact(
            "connector_id", "provider_account_id", "action_type",
            "resource_id", "contract_version",
        ) + """)
)""").format(s=s),

        sql.SQL("""
CREATE TABLE {s}.safeact_login_attempts (
    attempt_id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    username_key char(64) NOT NULL,
    principal_id uuid
        REFERENCES {s}.safeact_principals (principal_id)
        ON DELETE RESTRICT,
    source varchar(64) NOT NULL,
    outcome varchar(16) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_safeact_login_attempts_key
        CHECK (username_key ~ '^[0-9a-f]{{64}}$'),
    CONSTRAINT ck_safeact_login_attempts_outcome CHECK (outcome IN (
        'success', 'bad_credentials', 'unknown_user', 'locked', 'invalid'
    )),
    CONSTRAINT ck_safeact_login_attempts_success
        CHECK (outcome <> 'success' OR principal_id IS NOT NULL)
)""").format(s=s),

        sql.SQL("""
CREATE TABLE {s}.safeact_sessions (
    session_id uuid PRIMARY KEY,
    principal_id uuid NOT NULL
        REFERENCES {s}.safeact_principals (principal_id)
        ON DELETE RESTRICT,
    secret_sha256 bytea NOT NULL,
    csrf_sha256 bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    CONSTRAINT uq_safeact_sessions_secret UNIQUE (secret_sha256),
    CONSTRAINT ck_safeact_sessions_hashes CHECK (
        octet_length(secret_sha256) = 32
        AND octet_length(csrf_sha256) = 32
        AND secret_sha256 <> csrf_sha256
    ),
    CONSTRAINT ck_safeact_sessions_expiry CHECK (
        expires_at > created_at
        AND expires_at <= created_at + interval {absolute}
    )
)""").format(s=s, absolute=sql.Literal(SESSION_ABSOLUTE)),

        sql.SQL("""
CREATE TABLE {s}.safeact_session_activity (
    activity_id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    session_id uuid NOT NULL
        REFERENCES {s}.safeact_sessions (session_id)
        ON DELETE RESTRICT,
    seen_at timestamptz NOT NULL DEFAULT now()
)""").format(s=s),

        sql.SQL("""
CREATE TABLE {s}.safeact_session_revocations (
    session_id uuid PRIMARY KEY
        REFERENCES {s}.safeact_sessions (session_id)
        ON DELETE RESTRICT,
    reason varchar(32) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_safeact_session_revocations_reason
        CHECK (reason IN ('logout'))
)""").format(s=s),

        sql.SQL(
            "CREATE INDEX ix_safeact_login_attempts_principal "
            "ON {s}.safeact_login_attempts (principal_id, created_at)"
        ).format(s=s),
        sql.SQL(
            "CREATE INDEX ix_safeact_login_attempts_source "
            "ON {s}.safeact_login_attempts (source, created_at)"
        ).format(s=s),
        sql.SQL(
            "CREATE INDEX ix_safeact_session_activity_session "
            "ON {s}.safeact_session_activity (session_id, seen_at)"
        ).format(s=s),
    ]


def _guards(s):
    return [
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
    ] + [
        sql.SQL(
            "CREATE TRIGGER {trigger} BEFORE INSERT "
            "ON {s}.{table} FOR EACH ROW "
            "EXECUTE FUNCTION {s}.safeact_identity_guard()"
        ).format(
            s=s,
            table=sql.Identifier(table),
            trigger=sql.Identifier(table + "_identity_guard"),
        )
        for table in (
            "safeact_principals", "safeact_credentials",
            "safeact_role_grants",
        )
    ]


def _identity_guard(s):
    # A trigger, not a CHECK: the rule depends on the table's own
    # schema (demo principals only in the demo and test schemas) and on
    # the principal's kind, which lives in another table.
    return _function(s, "safeact_identity_guard", "", "trigger", """
DECLARE
    v_kind text;
BEGIN
    IF TG_TABLE_NAME = 'safeact_principals' THEN
        IF NEW.demo AND NOT (
            TG_TABLE_SCHEMA = 'safeact_demo'
            OR TG_TABLE_SCHEMA ~ '^safeact_it_[0-9a-f]{{32}}$'
        ) THEN
            RAISE EXCEPTION 'Demo principals are not allowed here'
                USING ERRCODE = {identity_invalid};
        END IF;
        RETURN NEW;
    END IF;

    EXECUTE format(
        'SELECT kind FROM %I.safeact_principals WHERE principal_id = $1',
        TG_TABLE_SCHEMA
    ) INTO v_kind USING NEW.principal_id;

    -- Nested IFs: plpgsql does not short-circuit, and NEW.role exists
    -- only on role grants.
    IF TG_TABLE_NAME = 'safeact_credentials' THEN
        IF v_kind IS DISTINCT FROM 'human' THEN
            RAISE EXCEPTION 'Only a human principal can hold a password'
                USING ERRCODE = {identity_invalid};
        END IF;
    ELSIF TG_TABLE_NAME = 'safeact_role_grants' THEN
        IF NEW.role = 'reviewer' AND v_kind IS DISTINCT FROM 'human' THEN
            RAISE EXCEPTION 'Only a human principal can be a reviewer'
                USING ERRCODE = {identity_invalid};
        END IF;
    END IF;
    RETURN NEW;
END;
""", definer=False)


def _password_matches(s):
    # The single place a bcrypt check runs. plpgsql (never inlined), so
    # tests can count calls per transaction.
    return _function(
        s, "safeact_password_matches", "p_password text, p_hash text",
        "boolean", """
BEGIN
    RETURN public.crypt(p_password, p_hash) = p_hash;
END;
""", definer=False)


def _session(s):
    return _function(
        s, "safeact_api_session",
        "p_secret text, p_csrf text, p_write boolean, p_role text",
        "TABLE (session_id uuid, principal_id uuid, tenant_id text, "
        "username text, display_name text, kind text, roles text[], "
        "demo boolean, expires_at timestamptz)", """
DECLARE
    v_session {s}.safeact_sessions%ROWTYPE;
    v_principal {s}.safeact_principals%ROWTYPE;
    v_last timestamptz;
    v_roles text[];
BEGIN
    IF p_secret IS NULL OR p_secret !~ '^[0-9a-f]{{64}}$' THEN
        RAISE EXCEPTION 'Not authenticated'
            USING ERRCODE = {unauthenticated};
    END IF;

    SELECT * INTO v_session FROM {s}.safeact_sessions AS ss
     WHERE ss.secret_sha256 = sha256(convert_to(p_secret, 'UTF8'));

    IF NOT FOUND
       OR v_session.expires_at <= now()
       OR EXISTS (
           SELECT 1 FROM {s}.safeact_session_revocations AS r
            WHERE r.session_id = v_session.session_id
       )
    THEN
        RAISE EXCEPTION 'Not authenticated'
            USING ERRCODE = {unauthenticated};
    END IF;

    SELECT max(a.seen_at) INTO v_last
      FROM {s}.safeact_session_activity AS a
     WHERE a.session_id = v_session.session_id;
    v_last := greatest(coalesce(v_last, v_session.created_at),
                       v_session.created_at);
    IF v_last + interval {idle} <= now() THEN
        RAISE EXCEPTION 'Not authenticated'
            USING ERRCODE = {unauthenticated};
    END IF;

    SELECT * INTO v_principal FROM {s}.safeact_principals AS p
     WHERE p.principal_id = v_session.principal_id;
    IF EXISTS (
        SELECT 1 FROM {s}.safeact_principal_disablements AS d
         WHERE d.principal_id = v_principal.principal_id
    ) THEN
        RAISE EXCEPTION 'Not authenticated'
            USING ERRCODE = {unauthenticated};
    END IF;

    IF p_write IS DISTINCT FROM false AND (
        p_csrf IS NULL
        OR p_csrf !~ '^[0-9a-f]{{64}}$'
        OR sha256(convert_to(p_csrf, 'UTF8')) <> v_session.csrf_sha256
    ) THEN
        RAISE EXCEPTION 'CSRF token refused'
            USING ERRCODE = {csrf_refused};
    END IF;

    SELECT coalesce(array_agg(g.role::text ORDER BY g.role), '{{}}')
      INTO v_roles
      FROM {s}.safeact_role_grants AS g
     WHERE g.principal_id = v_principal.principal_id;

    IF p_role IS NOT NULL AND NOT (p_role = ANY (v_roles)) THEN
        RAISE EXCEPTION 'Role required'
            USING ERRCODE = {role_required};
    END IF;

    IF v_last < now() - interval {touch} THEN
        INSERT INTO {s}.safeact_session_activity (session_id)
        VALUES (v_session.session_id);
    END IF;

    RETURN QUERY SELECT
        v_session.session_id, v_principal.principal_id,
        v_principal.tenant_id::text, v_principal.username::text,
        v_principal.display_name::text, v_principal.kind::text, v_roles,
        v_principal.demo, v_session.expires_at;
END;
""", definer=False)


def _login(s):
    return _function(
        s, "safeact_api_login",
        "p_username text, p_password text, p_source text",
        "TABLE (ok boolean, session_secret text, csrf_token text, "
        "expires_at timestamptz)", """
DECLARE
    v_name text := lower(coalesce(left(p_username, 256), ''));
    v_key text;
    v_source text := left(coalesce(p_source, ''), 64);
    v_valid boolean;
    v_principal uuid;
    v_hash text;
    v_match boolean;
    v_locked boolean := false;
    v_outcome text;
    v_secret text;
    v_csrf text;
    v_expires timestamptz;
BEGIN
    v_valid := p_username IS NOT NULL AND p_password IS NOT NULL
        AND length(p_username) BETWEEN 1 AND 64
        AND octet_length(p_password) BETWEEN 1 AND {max_password};
    v_key := encode(sha256(convert_to(v_name, 'UTF8')), 'hex');

    -- One login at a time per username, so concurrent guesses are all
    -- counted before any of them is checked.
    PERFORM pg_advisory_xact_lock(
        hashtextextended('safeact_login:' || v_key, 0)
    );

    IF v_valid THEN
        SELECT p.principal_id INTO v_principal
          FROM {s}.safeact_principals AS p
         WHERE p.username = v_name AND p.kind = 'human'
           AND NOT EXISTS (
               SELECT 1 FROM {s}.safeact_principal_disablements AS d
                WHERE d.principal_id = p.principal_id
           );
        IF v_principal IS NOT NULL THEN
            SELECT c.password_hash INTO v_hash
              FROM {s}.safeact_credentials AS c
             WHERE c.principal_id = v_principal;
        END IF;
    END IF;

    IF v_principal IS NOT NULL THEN
        v_locked := (
            SELECT count(*) FROM {s}.safeact_login_attempts AS a
             WHERE a.principal_id = v_principal
               AND a.outcome = 'bad_credentials'
               AND a.created_at > now() - interval {window}
               AND a.created_at > coalesce((
                   SELECT max(w.created_at)
                     FROM {s}.safeact_login_attempts AS w
                    WHERE w.principal_id = v_principal
                      AND w.outcome = 'success'
               ), '-infinity')
        ) >= {account_failures};
    END IF;
    v_locked := v_locked OR (
        SELECT count(*) FROM {s}.safeact_login_attempts AS a
         WHERE a.source = v_source
           AND a.outcome IN ('bad_credentials', 'unknown_user', 'invalid')
           AND a.created_at > now() - interval {window}
    ) >= {source_failures};

    -- Exactly one bcrypt check on every path: an unknown, locked or
    -- invalid login costs the same as a wrong password.
    v_match := {s}.safeact_password_matches(
        coalesce(left(p_password, 256), ''), coalesce(v_hash, {dummy})
    );

    v_outcome := CASE
        WHEN NOT v_valid THEN 'invalid'
        WHEN v_locked THEN 'locked'
        WHEN v_principal IS NULL THEN 'unknown_user'
        WHEN v_hash IS NULL OR NOT v_match THEN 'bad_credentials'
        ELSE 'success'
    END;

    INSERT INTO {s}.safeact_login_attempts
        (username_key, principal_id, source, outcome)
    VALUES (v_key, v_principal, v_source, v_outcome);

    IF v_outcome <> 'success' THEN
        RETURN QUERY SELECT false, NULL::text, NULL::text,
                            NULL::timestamptz;
        RETURN;
    END IF;

    v_secret := encode(public.gen_random_bytes(32), 'hex');
    v_csrf := encode(public.gen_random_bytes(32), 'hex');
    v_expires := now() + interval {absolute};

    INSERT INTO {s}.safeact_sessions (
        session_id, principal_id, secret_sha256, csrf_sha256, expires_at
    ) VALUES (
        gen_random_uuid(), v_principal,
        sha256(convert_to(v_secret, 'UTF8')),
        sha256(convert_to(v_csrf, 'UTF8')),
        v_expires
    );

    RETURN QUERY SELECT true, v_secret, v_csrf, v_expires;
END;
""")


def _logout(s):
    return _function(
        s, "safeact_api_logout", "p_secret text, p_csrf text", "boolean",
        """
DECLARE
    v_session uuid;
BEGIN
    SELECT x.session_id INTO v_session
      FROM {s}.safeact_api_session(p_secret, p_csrf, true, NULL) AS x;
    INSERT INTO {s}.safeact_session_revocations (session_id, reason)
    VALUES (v_session, 'logout')
    ON CONFLICT (session_id) DO NOTHING;
    RETURN true;
END;
""")


def _whoami(s):
    return _function(
        s, "safeact_api_whoami", "p_secret text",
        "TABLE (principal_id uuid, tenant_id text, username text, "
        "display_name text, kind text, roles text[], demo boolean, "
        "expires_at timestamptz)", """
BEGIN
    RETURN QUERY SELECT x.principal_id, x.tenant_id, x.username,
                        x.display_name, x.kind, x.roles, x.demo,
                        x.expires_at
      FROM {s}.safeact_api_session(p_secret, NULL, false, NULL) AS x;
END;
""")


def _signature(s, name, arguments):
    return sql.SQL("{}.{}({})").format(
        s, sql.Identifier(name), sql.SQL(arguments)
    )


def upgrade_statements(schema):
    s = v0003._schema(schema)
    return _tables(s) + [
        _identity_guard(s),
        _password_matches(s),
        _session(s),
        _login(s),
        _logout(s),
        _whoami(s),
    ] + _guards(s) + [
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
        sql.SQL("DROP FUNCTION IF EXISTS {}").format(
            _signature(s, name, arguments)
        )
        for name, arguments in reversed(tuple(NEW_FUNCTIONS.items()))
        if name != "safeact_identity_guard"
    ] + [
        sql.SQL("DROP TABLE IF EXISTS {}.{}").format(
            s, sql.Identifier(table)
        )
        for table in reversed(NEW_TABLES)
    ] + [
        sql.SQL("DROP FUNCTION IF EXISTS {}").format(
            _signature(s, "safeact_identity_guard", "")
        ),
    ]
