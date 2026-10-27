"""Independent committed DB observations; query intervals use the host's clock.

Lease coverage brackets the database's own live/expired answers inside a
provider request. No wall-clock offset between PostgreSQL and the host is
assumed. Audit transaction ids establish committed visibility, not the time
or loss of a commit acknowledgement.
"""

from connectors.http_trace import correlation_hash


def _interval(started_ns, finished_ns):
    if (type(started_ns) is not int or type(finished_ns) is not int
            or not 0 < started_ns <= finished_ns):
        raise ValueError("Invalid database observation interval")
    return dict(source="database", sample_started_ns=started_ns,
                sample_finished_ns=finished_ns, monotonic_ns=finished_ns)


def lease_rows(rows, started_ns, finished_ns):
    timing = _interval(started_ns, finished_ns)
    result = []
    for operation, seq, claim, attempt, expires, active, connector, key in rows:
        if type(active) is not bool or not claim or not attempt or expires is None:
            raise ValueError("Incomplete persisted dispatch lease")
        result.append(dict(timing, event="db_lease_sample", operation_id=str(operation),
            transition_seq=int(seq), claim_id=str(claim), attempt_id=str(attempt),
            lease_expires_at=float(expires), lease_active=active,
            provider=connector.removeprefix("mock-"), correlation_sha256=correlation_hash(key)))
    return result


def audit_rows(rows, started_ns, finished_ns):
    timing = _interval(started_ns, finished_ns)
    result = []
    for audit_id, operation, kind, xid, created in rows:
        if not str(xid).isdigit() or int(xid) < 1:
            raise ValueError("Missing committed audit transaction id")
        result.append(dict(timing, event="db_audit_committed", audit_id=int(audit_id),
            operation_id=str(operation), event_type=kind, commit_xid=str(xid),
            created_at=float(created)))
    return result


def recovery_rows(rows, started_ns, finished_ns):
    """The database compares its own deadline with its own observed_at.

    Host monotonic intervals establish when the committed answer was read;
    PostgreSQL wall times are retained for identity, never converted to host time.
    """
    timing = _interval(started_ns, finished_ns)
    result = []
    for operation, seq, state, connector, key, deadline, passed, escalated in rows:
        if (type(seq) is not int or seq < 0 or not key
                or (deadline is None) != (passed is None)
                or (passed is not None and type(passed) is not bool)
                or (state == "ESCALATED_UNRESOLVED" and escalated is None)):
            raise ValueError("Incomplete committed recovery sample")
        result.append(dict(timing, event="db_recovery_sample", operation_id=str(operation),
            transition_seq=seq, state=state, provider=connector.removeprefix("mock-"),
            correlation_sha256=correlation_hash(key),
            investigation_deadline=float(deadline) if deadline is not None else None,
            deadline_passed=passed,
            escalated_at=float(escalated) if escalated is not None else None))
    return result


def breaker_rows(rows, started_ns, finished_ns):
    timing = _interval(started_ns, finished_ns)
    result = []
    for op, seq, state, connector, key, account, failures, threshold, until, active in rows:
        if (type(seq) is not int or seq < 0 or not key or not account
                or type(failures) is not int or failures < 0 or type(threshold) is not int or threshold < 1
                or until is None or type(active) is not bool):
            raise ValueError('Incomplete committed breaker observation')
        result.append(dict(timing, event='db_breaker_sample', operation_id=str(op),
            transition_seq=seq, state=state, provider=connector.removeprefix('mock-'),
            correlation_sha256=correlation_hash(key), provider_account_id=account,
            consecutive_failures=failures, breaker_threshold=threshold,
            open_until=float(until), breaker_open=active))
    return result


def sample_breakers(connection, schema, local_ids):
    import time
    from psycopg import sql
    started = time.monotonic_ns()
    rows = connection.execute(sql.SQL('''
        SELECT o.operation_id, s.seq, s.state, o.connector_id,
               coalesce(o.provider_idempotency_key, o.operation_id::text), o.provider_account_id,
               h.consecutive_failures, k.breaker_threshold,
               extract(epoch FROM h.open_until), h.open_until > clock_timestamp()
        FROM {s}.safeact_operations o
        JOIN {s}.safeact_state_projection s USING (operation_id)
        JOIN {s}.safeact_connector_health h USING (connector_id, provider_account_id)
        JOIN {s}.safeact_contract_limits k ON k.connector_id=o.connector_id AND k.version=o.contract_version
        WHERE o.operation_id=ANY(%s::uuid[]) AND h.open_until IS NOT NULL
    ''').format(s=sql.Identifier(schema)), (local_ids,)).fetchall()
    return breaker_rows(rows, started, time.monotonic_ns())


def sample_recovery(connection, schema, local_ids):
    """Read the established SQL status API on an independent connection."""
    import time
    from psycopg import sql
    from storage.durable_lifecycle import investigation_status

    result = []
    for local_id in local_ids:
        started = time.monotonic_ns()
        binding = connection.execute(sql.SQL(
            "SELECT connector_id, coalesce(provider_idempotency_key, operation_id::text) "
            "FROM {}.safeact_operations WHERE operation_id = %s"
        ).format(sql.Identifier(schema)), (local_id,)).fetchone()
        if binding is None:
            raise RuntimeError("Admitted operation is missing from the independent recovery query")
        status = investigation_status(connection, schema, local_id)
        deadline, escalated = status.investigation_deadline, status.escalated_at
        result.extend(recovery_rows([(local_id, status.seq, status.state.value, *binding,
            deadline.timestamp() if deadline is not None else None,
            status.observed_at >= deadline if deadline is not None else None,
            escalated.timestamp() if escalated is not None else None)], started, time.monotonic_ns()))
    return result
