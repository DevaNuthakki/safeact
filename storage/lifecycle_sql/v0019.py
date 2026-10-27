"""FROZEN once committed: SQL for revision 0019_next_check.

When the next check will happen (Milestone 5, M5.6).

safeact_next_check(op) returns a JSON object the console and the MCP
tools show next to an operation's state:

  kind        what happens next (below)
  not_before  the earliest time it can happen, or null
  deadline    the investigation deadline, when one applies
  basis       why, in words

  none_terminal         SUCCEEDED, FAILED_CONFIRMED, CANCELLED_BEFORE_CLAIM
  awaiting_dispatch     RECORDED: a worker claims it next; not_before is
                        the provider breaker's open_until while it is open
  awaiting_retry        RETRY_ELIGIBLE: not_before is the recorded backoff
  dispatch_in_progress  CLAIMED/DISPATCHING: not_before is the lease end;
                        if the worker has not reported by then, the
                        reconciler investigates
  awaiting_approval     UNKNOWN/VERIFYING on a reviewed contract waiting
                        for a retry approval: until the deadline
  scheduled_lookup      UNKNOWN/VERIFYING: not_before is the next paced
                        lookup (null: the contract declares no pacing, so
                        at the next reconciler pass)
  post_escalation_lookup  ESCALATED: the next bounded lookup
  beyond_horizon        ESCALATED, past the post-escalation horizon: no
                        automatic lookup; only a reviewer's REVERIFY
  manual_review_only    ESCALATED with no lookup possible (for example
                        email): a person decides

Every time is a not-before: the reconciler runs at or after it (its own
poll interval applies), never before. Nothing here decides anything; it
reads the same rules the protected functions enforce.

safeact_api_operation (the 0013 detail view) gains 'next_check'.

Pinned by tests/phase_a/test_migration_sql_frozen.py.
"""

from psycopg import sql

from storage.lifecycle_sql import v0003, v0013


REVISION = "0019_next_check"

NEW_TABLES = ()
HISTORY_TABLES = ()
NEW_INDEXES = ()
LEGAL_TRANSITIONS = frozenset()

NEW_FUNCTIONS = {"safeact_next_check": "uuid"}
REPLACED_FUNCTIONS = ("safeact_api_operation",)

SHARED_FUNCTIONS = {}
WORKER_FUNCTIONS = {}
ADVANCER_FUNCTIONS = {}
RECOVERY_FUNCTIONS = {}
INTERNAL_FUNCTIONS = {"safeact_next_check": "uuid"}
OWNER_TABLE_PRIVILEGES = {}

_HEADER = (
    "LANGUAGE plpgsql SECURITY INVOKER "
    "SET search_path = pg_catalog, pg_temp "
    "AS $safeact_lifecycle$ "
)


def _next_check(s):
    return sql.SQL(
        "CREATE FUNCTION {s}.{name}(p_operation uuid) "
        "RETURNS jsonb STABLE " + _HEADER + """
DECLARE
    v_state text;
    v_lease timestamptz;
    v_status record;
    v_contract record;
    v_bounds text;
    v_pacing record;
    v_breaker timestamptz;
    v_retry timestamptz;
BEGIN
    SELECT p.state, p.lease_expires_at INTO v_state, v_lease
      FROM {s}.safeact_state_projection p
     WHERE p.operation_id = p_operation;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    IF v_state IN ('SUCCEEDED', 'FAILED_CONFIRMED',
                   'CANCELLED_BEFORE_CLAIM') THEN
        RETURN jsonb_build_object('kind', 'none_terminal',
            'not_before', NULL, 'deadline', NULL,
            'basis', 'finished: no further check');
    END IF;

    SELECT * INTO v_pacing FROM {s}.safeact_pacing(p_operation);

    IF v_state = 'RECORDED' THEN
        v_breaker := CASE WHEN v_pacing.breaker_open_until
                               > v_pacing.observed_at
                          THEN v_pacing.breaker_open_until END;
        RETURN jsonb_build_object('kind', 'awaiting_dispatch',
            'not_before', v_breaker, 'deadline', NULL,
            'basis', CASE WHEN v_breaker IS NULL
                THEN 'a worker claims it next'
                ELSE 'the provider breaker is open; claims resume after it'
                END);
    END IF;

    IF v_state = 'RETRY_ELIGIBLE' THEN
        v_retry := v_pacing.retry_not_before;
        RETURN jsonb_build_object('kind', 'awaiting_retry',
            'not_before', v_retry, 'deadline', NULL,
            'basis', CASE WHEN v_retry IS NULL
                THEN 'a protected retry, claimable now'
                ELSE 'a protected retry after its recorded backoff' END);
    END IF;

    IF v_state IN ('CLAIMED', 'DISPATCHING') THEN
        RETURN jsonb_build_object('kind', 'dispatch_in_progress',
            'not_before', v_lease, 'deadline', NULL,
            'basis', 'if the worker has not reported by the end of its '
                     'lease, the reconciler investigates');
    END IF;

    SELECT * INTO v_status FROM {s}.safeact_investigation_status(p_operation);

    IF v_state IN ('UNKNOWN', 'VERIFYING') THEN
        IF {s}.safeact_retry_approval_status(p_operation) = 'awaiting' THEN
            RETURN jsonb_build_object('kind', 'awaiting_approval',
                'not_before', v_status.investigation_deadline,
                'deadline', v_status.investigation_deadline,
                'basis', 'a reviewer must approve the retry; without it '
                         'the operation is decided at the deadline');
        END IF;
        RETURN jsonb_build_object('kind', 'scheduled_lookup',
            'not_before', v_pacing.next_lookup_at,
            'deadline', v_status.investigation_deadline,
            'basis', CASE WHEN v_pacing.next_lookup_at IS NULL
                THEN 'a provider lookup at the next reconciler pass'
                ELSE 'the next paced provider lookup' END);
    END IF;

    -- ESCALATED_UNRESOLVED
    SELECT k.post_escalation_check_interval_seconds AS interval_seconds
      INTO v_contract
      FROM {s}.safeact_operations o
      JOIN {s}.safeact_connector_contracts k
        ON k.connector_id = o.connector_id AND k.version = o.contract_version
     WHERE o.operation_id = p_operation;
    v_bounds := {s}.safeact_verification_bounds(p_operation);

    IF v_bounds IS NULL OR v_bounds = 'too_soon' THEN
        RETURN jsonb_build_object('kind', 'post_escalation_lookup',
            'not_before', CASE WHEN v_bounds = 'too_soon'
                THEN v_status.last_lookup_at
                     + make_interval(secs => v_contract.interval_seconds)
                ELSE v_status.observed_at END,
            'deadline', NULL,
            'basis', 'a bounded lookup after escalation; decisive '
                     'evidence reopens verification');
    END IF;
    IF v_bounds = 'beyond_horizon' THEN
        RETURN jsonb_build_object('kind', 'beyond_horizon',
            'not_before', NULL, 'deadline', NULL,
            'basis', 'past the post-escalation horizon: no automatic '
                     'lookup; a reviewer can approve one REVERIFY');
    END IF;
    RETURN jsonb_build_object('kind', 'manual_review_only',
        'not_before', NULL, 'deadline', NULL,
        'basis', 'no provider lookup is possible for this contract: a '
                 'person decides');
END; $safeact_lifecycle$""").format(
        s=s, name=sql.Identifier("safeact_next_check"))


def _operation(s):
    # The frozen 0013 detail view, plus 'next_check'.
    base = v0013._operation(s).as_string(None)
    top = "'outcome_class', CASE"
    if base.count(top) != 1:
        raise RuntimeError("0013 operation view changed shape")
    added = sql.SQL(
        "'next_check', {s}.safeact_next_check(o.operation_id),\n        "
    ).format(s=s).as_string(None)
    return sql.SQL(base.replace(top, added + top))


def _signature(s, name, arguments):
    return sql.SQL("{}.{}({})").format(
        s, sql.Identifier(name), sql.SQL(arguments)
    )


def upgrade_statements(schema):
    s = v0003._schema(schema)
    return [_next_check(s), _operation(s)] + [
        sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(
            _signature(s, name, arguments)
        )
        for name, arguments in NEW_FUNCTIONS.items()
    ]


def downgrade_statements(schema):
    s = v0003._schema(schema)
    return [v0013._operation(s)] + [
        sql.SQL("DROP FUNCTION IF EXISTS {}").format(
            _signature(s, name, arguments)
        )
        for name, arguments in NEW_FUNCTIONS.items()
    ]
