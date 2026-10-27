"""Coverage credited only when the raw evidence proves the interleaving.

HTTP results and provider observations are joined by correlation digests and
monotonic timestamps. Ambiguous concurrent attribution receives no credit.
Broker, lease, commit and process coverage requires its own direct observations.
A configured fault, state name or reveal command is never proof of a fault.
"""

import os

CRITICAL = {
    "reviewers_serialized_on_binding": "two independently authenticated reviewers overlap on the operation lock; one decision commits and replays",
    "stale_review_refused_after_evidence": "new provider evidence commits before an old displayed binding is refused and audited",
    "reviewed_retry_after_proven_nonapplication": "a version-bound human decision releases a retry after actual provider rejection",
    "agent_credential_rotation_collapsed": "two credentials for the same service principal admit and replay one operation",
    "agent_self_approval_refused": "MCP and human decision endpoints refuse agent approval without changing protected state",
    "agent_boundary_attacks_refused": "changed identity, payload, intent, revoked keys and cross-tenant access are refused with independent before/after observations",
    "effect_applied_response_lost": "effect committed; caller received no answer",
    "server_error_after_commit": "5xx response after the effect committed",
    "late_commit_after_empty_lookup": "empty lookup before the effect committed",
    "lease_expired_while_request_ran": "lease expired inside the request interval",
    "worker_crashed_after_send": "observed process death after the provider call",
    "worker_crashed_before_send": "observed process death before the provider call",
    "commit_ack_lost": "commit observed independently of its lost acknowledgement",
    "broker_messages_lost": "observed messages removed before work was claimed",
    "broker_process_killed": "queued messages lost across an owned Redis SIGKILL and restart",
    "broker_redelivered_during_dispatch": "the same unacked task restored while its original provider call ran",
    "duplicate_submissions_collapsed": "identical submissions returned one local id",
    "changed_payload_refused": "changed submission refused after original admission",
    "rate_limited_then_retried": "observed 429 response followed by another request",
    "provider_outage_spanned_deadline": "observed outage covered the deadline",
    'breaker_open_with_unresolved_work': 'committed provider-account breaker is open while failed work remains unresolved',
    "retention_expired_before_outcome": "observed key expiry before resolution",
    "lookup_lied_outside_contract": "lookup result violated the declared contract",
    "late_commit_after_escalation": "provider effect committed after an independently observed escalation",
    "compensation_applied_response_lost": "a separately bound refund applied after confirmed order failure, but its HTTP response was lost",
    "compensation_verified_by_lookup": "a completed refund lookup established the inverse before committed verified compensation",
    "compensation_unresolved_after_lost_response": "an applied refund remained unresolved after a lost HTTP response",
    "compensation_rejected_without_restoration": "a rejected refund remained unresolved, without a restoration claim",
}


def observed(raw):
    found, accepted = set(), {}
    for admission in raw.get("admissions", []):
        if admission.get("local_id") and not admission.get("changed"):
            accepted.setdefault(admission["index"], []).append(
                admission["local_id"])
    if any(len(ids) >= 2 and len(set(ids)) == 1 for ids in accepted.values()):
        found.add("duplicate_submissions_collapsed")
    if any(a.get("changed") and not a.get("local_id") and a.get("refused")
           and a["index"] in accepted for a in raw.get("admissions", [])):
        found.add("changed_payload_refused")
    cutoff = raw.get("observation_finished_monotonic_ns")
    evidence = dict(raw)
    if cutoff is not None:
        for key in ("ledger", "http", "database"):
            evidence[key] = [e for e in raw.get(key, [])
                             if isinstance(e.get("monotonic_ns"), int)
                             and e["monotonic_ns"] <= cutoff]
    found.update(_provider_interleavings(evidence))
    found.update(_lease_interleavings(evidence))
    found.update(_execution_interleavings(raw))
    found.update(_broker_interleavings(raw))
    found.update(_redelivery_interleavings(raw))
    from chaos.recovery_coverage import observed as recovery_observed
    found.update(recovery_observed(evidence))
    from chaos.workflow_coverage import observed as workflow_observed
    found.update(workflow_observed(evidence))
    from chaos.boundary_coverage import observed as boundary_observed
    found.update(boundary_observed(evidence))
    return found


def _wire_alias_groups(raw):
    """Actual bounded attempt identities, never merely matching business payloads."""
    from chaos.workflow_oracle import wire_keys

    arm = raw.get('arm')
    if arm not in ('b0', 'b1', 'b2'):
        return []
    admitted = {a['local_id'] for a in raw.get('admissions', []) if a.get('local_id')}
    records = raw.get('database', [])
    infrastructure = raw.get('infrastructure', {})
    if arm == 'b0':
        if infrastructure.get('protocol') != 'b0-process-v1' or infrastructure.get('database_created') is not False:
            return []
        records = infrastructure.get('process_snapshots', [])
    groups = {}
    for row in records:
        if row.get('operation_id') not in admitted or row.get('native_key_mode') != arm:
            continue
        if (type(row.get('sample_started_ns')) is not int or type(row.get('sample_finished_ns')) is not int
                or not 0 < row['sample_started_ns'] <= row['sample_finished_ns'] == row.get('monotonic_ns')
                or row['monotonic_ns'] > raw.get('observation_finished_monotonic_ns', float('inf'))):
            continue
        if arm == 'b0':
            pids = {p['pid'] for p in infrastructure.get('processes', []) if p.get('event') == 'b0_process_started'}
            if (row.get('source') != 'process' or row.get('event') != 'process_operation_sample'
                    or row.get('binding_format') != 'b0-operation-v1' or row.get('sender_pid') not in pids
                    or not any(a.get('source') == 'process' and a.get('durable') is False
                        and a.get('operation_id') == row['operation_id'] and a.get('pid') == row['sender_pid']
                        and type(a.get('sample_finished_ns')) is int and a['sample_finished_ns'] < row['sample_started_ns']
                        for a in infrastructure.get('acceptance', []))):
                continue
        elif row.get('source') != 'database' or row.get('event') != 'db_operation_sample':
            continue
        try:
            groups.setdefault(row['operation_id'], set()).update(wire_keys(row))
        except (KeyError, ValueError, TypeError):
            continue
    return list(groups.values())


def _redelivery_interleavings(raw, *, refusal=False):
    cutoff = raw.get("observation_finished_monotonic_ns", float("inf"))
    def timed(key, source):
        return [e for e in raw.get(key, []) if e.get("source") == source
                and type(e.get("monotonic_ns")) is int and e["monotonic_ns"] <= cutoff]
    broker = timed("broker", "broker")
    database = timed("database", "database")
    timeline = [e for e in raw.get("timeline", []) if type(e.get("monotonic_ns")) is int
                and e["monotonic_ns"] <= cutoff]
    admitted = {a["local_id"] for a in raw.get("admissions", [])
                if a.get("local_id") and not a.get("changed")}
    keys = ("operation_id", "outbox_id", "delivery_id", "task_id")
    def same(a, b):
        return all(a.get(k) and a.get(k) == b.get(k) for k in keys)
    hpairs = _pairs(timed("http", "http_client"), "http_start", {"http_response", "http_error"}, "request_id")
    ppairs = _pairs(timed("ledger", "provider"), "provider_start", {"provider_end", "provider_error"}, "provider_call_id")
    for store in timeline:
        if (store.get("event") != "broker_unacked_observed" or store.get("source") != "broker_store"
                or store.get("operation_id") not in admitted or store.get("redis_db") not in range(2, 9)
                or not store.get("delivery_tag") or not store.get("queue")):
            continue
        us, ue = store.get("sample_started_ns"), store.get("sample_finished_ns")
        if type(us) is not int or type(ue) is not int or not us <= ue <= store["monotonic_ns"]:
            continue
        mine = [e for e in broker if same(e, store) and e.get("queue") == store["queue"]]
        publications = [e for e in mine if e["event"] == "publish_started"]
        published = [e for e in mine if e["event"] == "publish_returned"]
        if (len(publications) != 1 or len(published) != 1
                or publications[0]["pid"] != published[0]["pid"]
                or not publications[0]["monotonic_ns"] < published[0]["monotonic_ns"] < us):
            continue
        transport = [e for e in timeline if same(e, store) and e.get("source") == "broker_transport"
            and e.get("method") == "QoS.restore_by_tag" and e.get("delivery_tag") == store["delivery_tag"]
            and e.get("queue") == store["queue"] and e.get("redis_db") == store["redis_db"]]
        requests = [e for e in transport if e["event"] == "broker_restore_requested"]
        returns = [e for e in transport if e["event"] == "broker_restore_returned"]
        if len(requests) != 1 or len(returns) != 1:
            continue
        request, returned = requests[0], returns[0]
        if not store["monotonic_ns"] < request["monotonic_ns"] < returned["monotonic_ns"]:
            continue
        samples = [e for e in database if same(e, store)
            and ((e.get("event")=="db_broker_dispatching" and e.get("state")=="DISPATCHING")
                or (e.get('event')=='db_broker_inflight_attempt' and e.get('proof_format')=='queue-attempt-v1'
                    and raw.get('arm') in ('b1','b2')
                    and e.get('attempt_outcome_pending') is True and e.get('lease_active') is True
                    and e.get('native_job_state') in ('DISPATCHING','RETRY_ELIGIBLE','UNKNOWN','SUCCEEDED','FAILED_CONFIRMED')))
            and e.get("claim_id") and e.get("attempt_id")
            and type(e.get("transition_seq")) is int and e["transition_seq"] > 0
            and type(e.get("sample_started_ns")) is int and type(e.get("sample_finished_ns")) is int
            and e["sample_started_ns"] <= e["sample_finished_ns"] <= e["monotonic_ns"]]
        for original in [e for e in mine if e["event"] == "task_started" and e.get("redelivered") is False
                         and e["monotonic_ns"] < us]:
            received = [e for e in mine if e["event"] == "task_received" and e.get("redelivered") is False
                        and publications[0]["monotonic_ns"] < e["monotonic_ns"] < original["monotonic_ns"]]
            if len(received) != 1:
                continue
            for duplicate in [e for e in mine if e["event"] == "task_started" and e.get("redelivered") is True
                              and e["pid"] != original["pid"] and request["monotonic_ns"] < e["monotonic_ns"]]:
                if not any(e["event"] == "task_received" and e.get("redelivered") is True
                    and request["monotonic_ns"] < e["monotonic_ns"] < duplicate["monotonic_ns"] for e in mine):
                    continue
                for before in samples:
                    if not us <= before["sample_started_ns"] <= before["sample_finished_ns"] < request["monotonic_ns"]:
                        continue
                    for after in samples:
                        if (not duplicate["monotonic_ns"] < after["sample_started_ns"]
                                or not returned["monotonic_ns"] < after["sample_started_ns"]
                                or any(before.get(k) != after.get(k) for k in
                                       ("transition_seq", "claim_id", "attempt_id", "provider", "correlation_sha256"))):
                            continue
                        for start, end in hpairs:
                            if (start.get("method") != "POST" or start.get("pid") != original["pid"]
                                    or start.get("provider") != before.get("provider")
                                    or start.get("correlation_sha256") != before.get("correlation_sha256")
                                    or not original["monotonic_ns"] < start["monotonic_ns"] < us
                                    or not after["sample_finished_ns"] < end["monotonic_ns"]):
                                continue
                            if any(p.get("method") in ("refund", "charge", "create_record", "send")
                                and p.get("provider") == start["provider"]
                                and p.get("correlation_sha256") == start["correlation_sha256"]
                                and start["monotonic_ns"] <= p["monotonic_ns"] < before["sample_started_ns"]
                                and after["sample_finished_ns"] < answer["monotonic_ns"] <= end["monotonic_ns"]
                                for p, answer in ppairs):
                                if refusal:
                                    if (raw.get('arm') in ('safeact','b3')
                                            and before.get('event') == after.get('event') == 'db_broker_dispatching'
                                            and before.get('lease_active') is True and after.get('lease_active') is True
                                            and any(e.get('event') == 'task_returned' and e.get('redelivered') is True
                                                and e.get('pid') == duplicate['pid'] and e.get('task_status') == 'not_claimable'
                                                and duplicate['monotonic_ns'] < e['monotonic_ns'] < after['sample_started_ns'] for e in mine)):
                                        return {'concurrent_reclaim_refused'}
                                else:
                                    return {"broker_redelivered_during_dispatch"}
    return set()


def boundary_details(raw):
    return dict(commit_ack_stages=sorted(_execution_interleavings(raw, stages=True)),
                concurrent_reclaim_refused=bool(_redelivery_interleavings(raw, refusal=True)))


def _broker_interleavings(raw):
    cutoff = raw.get("observation_finished_monotonic_ns", float("inf"))
    rows = [e for e in raw.get("broker", []) if e.get("source") == "broker"
            and type(e.get("monotonic_ns")) is int and e["monotonic_ns"] <= cutoff]
    timeline = [e for e in raw.get("timeline", []) if type(e.get("monotonic_ns")) is int
                and e["monotonic_ns"] <= cutoff]
    admitted = {a["local_id"] for a in raw.get("admissions", []) if a.get("local_id") and not a.get("changed")}
    keys = ("operation_id", "outbox_id", "delivery_id", "task_id")
    def same(a, b):
        return all(a.get(key) and a.get(key) == b.get(key) for key in keys)
    def publication(message, queue):
        mine = [e for e in rows if same(e, message) and e.get("queue") == queue]
        starts = [e for e in mine if e["event"] == "publish_started"]
        ends = [e for e in mine if e["event"] == "publish_returned"]
        if len(starts) == len(ends) == 1 and starts[0].get("pid") == ends[0].get("pid"):
            if starts[0]["monotonic_ns"] < ends[0]["monotonic_ns"]:
                return starts[0], ends[0]
        return None
    found = set()
    for flush, end, labels in _broker_loss_windows(raw, timeline):
        start = flush["sample_started_ns"]
        if not any(e["event"] == "broker_relay_stopped" and e["monotonic_ns"] < start
                   and type(e.get("wait_status")) is int and os.WIFSTOPPED(e["wait_status"])
                   and os.WSTOPSIG(e["wait_status"]) == e.get("stop_signal") == e.get("requested_signal")
                   for e in timeline):
            continue
        for message in flush["messages"]:
            before = publication(message, flush.get("queue"))
            if message.get("operation_id") not in admitted or before is None or before[1]["monotonic_ns"] >= start:
                continue
            if not any(e["event"] == "broker_consumer_stopped" and e.get("returncode") == -9
                       and e["monotonic_ns"] < before[0]["monotonic_ns"] for e in timeline):
                continue
            if not any(e["event"] == "broker_consumers_disconnected" and e.get("redis_db") == flush["redis_db"]
                       and e["monotonic_ns"] < before[0]["monotonic_ns"] for e in timeline):
                continue
            pending = [e for e in raw.get("database", []) if e.get("source") == "database"
                and e.get("event") == "db_broker_pending" and e.get("state") == "RECORDED"
                and all(e.get(key) == message.get(key) for key in keys[:3])
                and type(e.get("sample_started_ns")) is int and type(e.get("sample_finished_ns")) is int
                and before[1]["monotonic_ns"] < e["sample_started_ns"] <= e["sample_finished_ns"] < start]
            if not pending or any(same(e, message) and e["event"] in ("task_received", "task_started")
                                  and e["monotonic_ns"] <= end for e in rows):
                continue
            for later in rows:
                if (later["event"] != "publish_started" or later.get("operation_id") != message["operation_id"]
                        or later.get("outbox_id") != message["outbox_id"] or later.get("delivery_id") == message["delivery_id"]
                        or later.get("task_id") == message["task_id"] or later["monotonic_ns"] <= end):
                    continue
                after = publication(later, flush.get("queue"))
                if after is None:
                    continue
                received = [e for e in rows if e["event"] == "task_received" and same(e, later)
                            and e.get("queue") == flush.get("queue") and later["monotonic_ns"] < e["monotonic_ns"]]
                if any(same(e, later) and e["event"] == "task_started" and e.get("queue") == flush.get("queue")
                       and any(r["monotonic_ns"] <= e["monotonic_ns"] for r in received) for e in rows):
                    found.update(labels)
    return found


def _broker_loss_windows(raw, timeline):
    """A flush and a verified process restart are different loss mechanisms."""
    for row in timeline:
        if (row.get("event") != "broker_messages_flushed" or row.get("source") != "broker_store"
                or type(row.get("redis_db")) is not int or row["redis_db"] not in range(2, 9)
                or row.get("acknowledged") is not True or row.get("after_count") != 0
                or row.get("database_size_after") != 0 or not row.get("messages")
                or row.get("before_count") != len(row["messages"])):
            continue
        start, end = row.get("sample_started_ns"), row.get("sample_finished_ns")
        if type(start) is int and type(end) is int and start <= end <= row["monotonic_ns"]:
            yield row, end, {"broker_messages_lost"}
    for queued in timeline:
        if (queued.get("event") != "isolated_redis_queued" or queued.get("source") != "isolated_broker"
                or type(queued.get("redis_db")) is not int or queued["redis_db"] not in range(2, 9)
                or not queued.get("messages") or queued.get("before_count") != len(queued["messages"])):
            continue
        identity = ("container_ref", "owner", "redis_db", "port")
        if any(not queued.get(k) for k in identity):
            continue
        mine = [e for e in timeline if all(e.get(k) == queued[k] for k in identity)
                and e.get("source") == "isolated_broker"]
        requests = [e for e in mine if e["event"] == "isolated_redis_kill_requested"]
        killed = [e for e in mine if e["event"] == "isolated_redis_killed"]
        restarted = [e for e in mine if e["event"] == "isolated_redis_restarted"]
        if len(requests) != 1 or len(killed) != 1 or len(restarted) != 1:
            continue
        request, death, after = requests[0], killed[0], restarted[0]
        start, end = queued.get("sample_started_ns"), queued.get("sample_finished_ns")
        rs, re = after.get("sample_started_ns"), after.get("sample_finished_ns")
        if (any(type(v) is not int for v in (start, end, rs, re))
                or not start <= end <= queued["monotonic_ns"] < request["monotonic_ns"]
                       < death["monotonic_ns"] < rs <= re <= after["monotonic_ns"]
                or request.get("signal") != 9 or death.get("signal") != 9
                or type(request.get("daemon_pid")) is not int or request["daemon_pid"] <= 1
                or death.get("wait_exit_code") != 137 or death.get("inspect_exit_code") != 137
                or death.get("oom_killed") is not False
                or any(type(death.get(k)) is not int or death[k] <= 0 for k in ("daemon_kill_ns", "daemon_die_ns"))
                or not queued.get("run_id") or queued["run_id"] != request.get("run_id")
                or queued["run_id"] != death.get("run_id") or queued["run_id"] != after.get("previous_run_id")
                or not after.get("run_id") or after["run_id"] == queued["run_id"]
                or after.get("queue") != queued.get("queue") or after.get("after_count") != 0
                or after.get("database_size_after") != 0):
            continue
        lifecycle = [e for e in raw.get("isolated_broker", []) if all(e.get(k) == queued[k] for k in identity)]
        origins = [e for e in lifecycle if e.get("event") == "isolated_redis_started"
                   and type(e.get("monotonic_ns")) is int and e["monotonic_ns"] < start
                   and e.get("run_id") == queued["run_id"] and e.get("image") == "redis:7.4.2-bookworm"
                   and e.get("image_id_prefix")]
        if len(origins) != 1:
            continue
        if any(e.get("aof_enabled") is not False or e.get("rdb_enabled") is not False
               or e.get("server_pid") != 1 or e.get("redis_version") != "7.4.2" for e in (origins[0], queued, after)):
            continue
        # Daemon event timestamps are not compared with the host clock or used
        # as kernel execution timestamps. Host query/command intervals order the fault.
        yield queued, re, {"broker_messages_lost", "broker_process_killed"}


def _pairs(events, first, last, identity):
    starts, endings = {}, {}
    for event in events:
        if event.get("event") == first:
            starts.setdefault(event.get(identity), []).append(event)
        if event.get("event") in last:
            key = event.get(identity)
            endings[key] = endings.get(key, 0) + 1
    pairs = []
    for end in events:
        candidates = starts.get(end.get(identity), [])
        if (end.get("event") not in last or len(candidates) != 1
                or endings.get(end.get(identity)) != 1):
            continue
        start = candidates[0]
        if (start.get(identity) and start.get("correlation_sha256")
                and start.get("provider") and start.get("method")
                and isinstance(start.get("monotonic_ns"), int)
                and isinstance(end.get("monotonic_ns"), int)
                and start["monotonic_ns"] <= end["monotonic_ns"]
                and all(start.get(key) == end.get(key) for key in (
                    "provider", "method", "path", "correlation_sha256"))):
            pairs.append((start, end))
    return pairs


def _execution_interleavings(raw, *, stages=False):
    cutoff = raw.get("observation_finished_monotonic_ns", float("inf"))
    execution = [e for e in raw.get("execution", []) if e.get("source") == "execution"
                 and type(e.get("monotonic_ns")) is int and e["monotonic_ns"] <= cutoff]
    admitted = {a["local_id"] for a in raw.get("admissions", [])
                if a.get("local_id") and not a.get("changed")}
    found = set()
    committed = {e.get("commit_xid") for e in raw.get("database", [])
                 if e.get("event") == "db_audit_committed" and e.get("source") == "database"
                 and e.get("operation_id") in admitted}
    for error in execution:
        if (error["event"] != "hook_error" or error.get("error_type") not in ("ConnectionError","LostAcknowledgement")
                or not error.get("commit_xid") or error["commit_xid"] not in committed
                or error.get("point") != f"after_{error.get('stage')}_commit"):
            continue
        same = [e for e in execution if e.get("pid") == error.get("pid")
                and e.get("stage") == error.get("stage")
                and e.get("commit_xid") == error["commit_xid"]]
        if any(before["event"] == "commit_before" and after["event"] == "commit_returned"
               and before["monotonic_ns"] < after["monotonic_ns"] < error["monotonic_ns"]
               for before in same for after in same):
            found.add(error['stage'] if stages else 'commit_ack_lost')
    if stages:
        return found
    http = [e for e in raw.get("http", []) if e.get("source") == "http_client"
            and e.get("monotonic_ns", cutoff + 1) <= cutoff]
    ledger = [e for e in raw.get("ledger", []) if e.get("source") == "provider"
              and e.get("monotonic_ns", cutoff + 1) <= cutoff]
    pairs = _pairs(http, "http_start", {"http_response", "http_error"}, "request_id")
    requests = {e["worker_pid"]: e for e in raw.get("timeline", [])
                if e.get("event") == "worker_kill_requested"}
    for killed in raw.get("timeline", []):
        if killed.get("event") != "worker_killed" or len(admitted) != 1:
            continue
        request = requests.get(killed.get("worker_pid"))
        if (request is None or killed.get("returncode") != -9 or killed.get("signal") != 9
                or request.get("process_group") != killed.get("worker_pid")
                or any(request.get(k) != killed.get(k) for k in ("point", "parked_pid", "signal"))
                or not request["monotonic_ns"] < killed["monotonic_ns"] <= cutoff):
            continue
        hooks = [e for e in execution if e["event"] == "hook_enter"
                 and e.get("point") == killed.get("point") and e.get("pid") == killed.get("parked_pid")
                 and e["monotonic_ns"] <= request["monotonic_ns"]]
        if not hooks:
            continue
        mine = [(s, e) for s, e in pairs if s.get("method") == "POST"
                and s.get("pid") == killed["parked_pid"]
                and e["monotonic_ns"] < request["monotonic_ns"]]
        if killed["point"] in ("after_provider_call", "before_outcome_commit"):
            for start, end in mine:
                key = start.get("correlation_sha256")
                calls = [e for e in ledger if e["event"] == "call" and e.get("correlation_sha256") == key]
                sent = [e for e in http if e["event"] == "http_start" and e.get("method") == "POST"
                        and e.get("correlation_sha256") == key]
                providers = [e for e in ledger if e["event"] == "provider_start"
                             and e.get("method") in ("refund", "charge", "create_record", "send")
                             and e.get("correlation_sha256") == key]
                if len(calls) == len(sent) == len(providers) == 1 and any(
                        e["event"] == "effect" and e.get("correlation_sha256") == key
                        and providers[0].get("provider") == start.get("provider")
                        and start["monotonic_ns"] <= providers[0]["monotonic_ns"]
                            <= calls[0]["monotonic_ns"] <= e["monotonic_ns"] <= end["monotonic_ns"]
                        for e in ledger):
                    found.add("worker_crashed_after_send")
        if (killed["point"] in ("after_claim_commit", "after_fence_commit","after_delivery_commit","before_provider_call")
                and not any(e.get("method") == "POST" and e.get("pid") == killed["parked_pid"]
                            and e["monotonic_ns"] <= request["monotonic_ns"] for e in http)
                and not any(e["event"] == "call" and e["monotonic_ns"] <= request["monotonic_ns"] for e in ledger)):
            found.add("worker_crashed_before_send")
    return found


def _lease_interleavings(raw):
    ledger = [e for e in raw.get("ledger", []) if e.get("source") == "provider"]
    http = [e for e in raw.get("http", []) if e.get("source") == "http_client"]
    hpairs = _pairs(http, "http_start", {"http_response", "http_error"}, "request_id")
    ppairs = _pairs(ledger, "provider_start", {"provider_end", "provider_error"}, "provider_call_id")
    groups = {}
    admitted = {a["local_id"] for a in raw.get("admissions", [])
                if a.get("local_id") and not a.get("changed")}
    for sample in raw.get("database", []):
        if (sample.get("event") != "db_lease_sample" or sample.get("source") != "database"
                or sample.get("operation_id") not in admitted
                or type(sample.get("lease_active")) is not bool):
            continue
        identity = tuple(sample.get(key) for key in (
            "operation_id", "transition_seq", "claim_id", "attempt_id", "lease_expires_at",
            "provider", "correlation_sha256"))
        if all(value is not None for value in identity):
            groups.setdefault(identity, []).append(sample)
    for start, end in ppairs:
        if start["method"] not in ("refund", "charge", "create_record", "send"):
            continue
        clients = [(h, answer) for h, answer in hpairs if h.get("method") == "POST"
            and h.get("provider") == start.get("provider")
            and h.get("correlation_sha256") == start.get("correlation_sha256")
            and h["monotonic_ns"] <= start["monotonic_ns"]
            and end["monotonic_ns"] <= answer["monotonic_ns"]]
        if len(clients) != 1:
            continue
        for samples in groups.values():
            inside = [s for s in samples
                if s.get("provider") == start["provider"]
                and s.get("correlation_sha256") == start["correlation_sha256"]
                and type(s.get("sample_started_ns")) is int
                and type(s.get("sample_finished_ns")) is int
                and start["monotonic_ns"] <= s["sample_started_ns"]
                    <= s["sample_finished_ns"] <= end["monotonic_ns"]]
            if any(before["lease_active"] and not after["lease_active"]
                   and before["sample_finished_ns"] < after["sample_started_ns"]
                   for before in inside for after in inside):
                return {"lease_expired_while_request_ran"}
    return set()


def _provider_interleavings(raw):
    from chaos.lookup_trace import empty_lookup_windows

    found = set()
    ledger = [e for e in raw.get("ledger", []) if e.get("source") == "provider"
              and isinstance(e.get("monotonic_ns"), int)]
    http = [e for e in raw.get("http", []) if e.get("source") == "http_client"]
    hpairs = _pairs(http, "http_start", {"http_response", "http_error"}, "request_id")
    ppairs = _pairs(ledger, "provider_start", {"provider_end", "provider_error"},
                    "provider_call_id")
    calls = [e for e in ledger if e["event"] == "call"]
    effects = [e for e in ledger if e["event"] == "effect"]
    aliases = _wire_alias_groups(raw)
    for window in empty_lookup_windows(raw):
        mine = [e for e in effects if e.get("correlation_sha256") == window["correlation_sha256"]]
        if mine and window["http_finished_ns"] < min(e["monotonic_ns"] for e in mine):
            found.add("late_commit_after_empty_lookup")
    for start, end in hpairs:
        if start.get("method") != "POST":
            continue
        key = start.get("correlation_sha256")
        mine = [e for e in calls if e.get("correlation_sha256") == key]
        hstarts = [e for e in http if e.get("event") == "http_start"
                   and e.get("method") == "POST" and e.get("correlation_sha256") == key]
        pstarts = [e for e in ledger if e["event"] == "provider_start"
                   and e.get("method") in ("refund", "charge", "create_record", "send")
                   and e.get("correlation_sha256") == key]
        # A shared key with multiple concurrent calls cannot identify which
        # call applied the effect. Such runs stay uncredited here.
        if len(mine) == len(hstarts) == len(pstarts) == 1:
            request = mine[0]
            inside = (pstarts[0].get("provider") == start.get("provider")
                      and start["monotonic_ns"] <= pstarts[0]["monotonic_ns"]
                      <= request["monotonic_ns"] <= end["monotonic_ns"])
            applied = any(e.get("correlation_sha256") == key
                and request["monotonic_ns"] <= e["monotonic_ns"] <= end["monotonic_ns"]
                for e in effects)
            if inside and applied:
                if end["event"] == "http_error" and end.get("error_type") in (
                    "ReadTimeout", "WriteTimeout", "ReadError", "WriteError",
                    "RemoteProtocolError", "TimeoutException"):
                    found.add("effect_applied_response_lost")
                if end["event"] == "http_response" and 500 <= end.get("status_code", 0) < 600:
                    found.add("server_error_after_commit")
        if end.get("status_code") == 429:
            if any(later.get("method") == "POST"
                   and later.get("provider") == start.get("provider")
                   and (later.get("correlation_sha256") == key or any(
                       key in group and later.get('correlation_sha256') in group for group in aliases))
                   and later["monotonic_ns"] > end["monotonic_ns"]
                   and any(e.get("correlation_sha256") == later.get('correlation_sha256')
                           and later["monotonic_ns"] <= e["monotonic_ns"] <= answer["monotonic_ns"]
                           for e in calls) for later, answer in hpairs):
                found.add("rate_limited_then_retried")
    for start, end in ppairs:
        if start.get("method") not in ("lookup", "search") or end["event"] != "provider_end":
            continue
        key = start.get("correlation_sha256")
        mine = [e for e in effects if e.get("correlation_sha256") == key]
        hashes = {e.get("payload_sha256") for e in calls
                  if e.get("correlation_sha256") == key and e.get("payload_sha256")}
        ids = {e.get("effect_id") for e in mine}
        if len(hashes) == 1 and any(r.get("effect_id") in ids
                and r.get("payload_sha256") and r["payload_sha256"] not in hashes
                for r in end.get("lookup_records", [])):
            found.add("lookup_lied_outside_contract")
    return found
