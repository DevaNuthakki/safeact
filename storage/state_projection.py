"""Check the current-state projection against the transition log.

The projection (0017) is derived from ``safeact_operation_transitions``.
``drift_query`` recomputes every operation's latest transition straight
from the log, the way the 0003 view did, and returns each operation
whose projection row is missing or differs in any column. An empty
result means the projection matches the log exactly.

Run by fixtures before they drop a schema, after qualification
scenarios and after load-test runs. Read-only; needs SELECT on the
operations, transitions and projection tables (an administrator).
"""

from dataclasses import dataclass

from psycopg import sql

from storage.lifecycle_sql.v0003 import _schema
from storage.lifecycle_sql.v0017 import PROJECTION_TABLE


_DRIFT = """
SELECT o.operation_id,
       p.seq, coalesce(t.seq, 0),
       p.state, coalesce(t.to_state, 'RECORDED')
FROM {s}.safeact_operations o
LEFT JOIN {s}.{p} p ON p.operation_id = o.operation_id
LEFT JOIN LATERAL (
    SELECT x.seq, x.to_state, x.claim_id, x.attempt_id, x.actor,
           x.lease_expires_at, x.created_at
    FROM {s}.safeact_operation_transitions x
    WHERE x.operation_id = o.operation_id
    ORDER BY x.seq DESC
    LIMIT 1
) t ON TRUE
WHERE p.operation_id IS NULL
   OR p.seq IS DISTINCT FROM coalesce(t.seq, 0)
   OR p.state IS DISTINCT FROM coalesce(t.to_state, 'RECORDED')
   OR p.claim_id IS DISTINCT FROM t.claim_id
   OR p.attempt_id IS DISTINCT FROM t.attempt_id
   OR p.actor IS DISTINCT FROM t.actor
   OR p.lease_expires_at IS DISTINCT FROM t.lease_expires_at
   OR p.state_changed_at IS DISTINCT FROM t.created_at
ORDER BY o.operation_id
"""


@dataclass(frozen=True)
class Drift:
    operation_id: object
    projected_seq: object
    log_seq: int
    projected_state: object
    log_state: str


def drift_query(schema):
    return sql.SQL(_DRIFT).format(
        s=_schema(schema), p=sql.Identifier(PROJECTION_TABLE)
    )


def projection_drift(connection, schema):
    """Every operation whose projection row disagrees with the log."""

    return [
        Drift(*row) for row in connection.execute(drift_query(schema))
    ]


def assert_no_drift(connection, schema):
    drift = projection_drift(connection, schema)
    if drift:
        raise AssertionError(
            f"Current-state projection drift in {schema}: "
            f"{len(drift)} operation(s), first {drift[:3]}"
        )
