"""Credit F14/F15 only from joined client, committed DB and provider evidence."""


def _timed(row, cutoff):
    a, b, c = (row.get(k) for k in ("sample_started_ns", "sample_finished_ns", "monotonic_ns"))
    return all(type(n) is int for n in (a, b, c)) and 0 < a <= b <= c <= cutoff


def _snapshots(raw):
    cutoff = raw.get("observation_finished_monotonic_ns", 0)
    return [s for s in raw.get("database", []) if s.get("source") == "database"
            and s.get("event") == "db_boundary_snapshot" and _timed(s, cutoff)]


def observed(raw):
    cutoff = raw.get("observation_finished_monotonic_ns", 0)
    calls = [e for e in raw.get("boundary", []) if _timed(e, cutoff)]
    samples = _snapshots(raw)
    found = set()
    for operation in {a["local_id"] for a in raw.get("admissions", []) if a.get("local_id")}:
        mine = [s for s in samples if s.get("operation_id") == operation]
        if not mine:
            continue
        key = mine[0].get("correlation_sha256")
        ledger = [e for e in raw.get("ledger", []) if e.get("source") == "provider"
                  and e.get("correlation_sha256") == key and type(e.get("monotonic_ns")) is int
                  and e["monotonic_ns"] <= cutoff]
        effects = [e for e in ledger if e.get("event") == "effect"]
        dispatches = [e for e in ledger if e.get("event") == "provider_start" and e.get("method") == "create_record"]
        if len(effects) != 1 or not any(s["state"] == "SUCCEEDED" and s["sample_started_ns"] > effects[0]["monotonic_ns"] for s in mine):
            continue
        verified = _verified_samples(raw, mine, ledger, effects[0], cutoff)
        # Protected retry can succeed on the worker's direct positive response;
        # ambiguity/reverify cases require an actual successful provider lookup.
        if not verified and not any(p.get("action") == "PROTECTED_RETRY" for s in mine for p in s.get("proposals", [])):
            continue
        mine = [dict(s, lookup_confirmed=s["sample_finished_ns"] in verified) for s in mine]
        found.update(_review(operation, mine, calls, ledger, dispatches))
        found.update(_agent(operation, mine, calls, ledger, dispatches))
    return found


def _verified_samples(raw, samples, ledger, effect, cutoff):
    from chaos.coverage import _pairs
    http = [e for e in raw.get("http", []) if e.get("source") == "http_client"
            and type(e.get("monotonic_ns")) is int and e["monotonic_ns"] <= cutoff]
    provider_pairs = _pairs(ledger, "provider_start", {"provider_end", "provider_error"}, "provider_call_id")
    http_pairs = _pairs(http, "http_start", {"http_response", "http_error"}, "request_id")
    hashes = {e.get("payload_sha256") for e in ledger if e.get("event") == "call" and e.get("payload_sha256")}
    verified = set()
    if len(hashes) != 1 or not effect.get("effect_id"):
        return verified
    expected = [dict(effect_id=effect["effect_id"], payload_sha256=next(iter(hashes)))]
    for p, answer in provider_pairs:
        if p.get("method") != "search" or answer.get("lookup_records") != expected:
            continue
        exchanges = [(start, end) for start, end in http_pairs if start.get("method") == "GET"
            and start.get("path") == "/records" and start.get("provider") == p.get("provider") == "crm"
            and start.get("correlation_sha256") == p.get("correlation_sha256")
            and end.get("status_code") == 200 and end.get("event") == "http_response"
            and effect["monotonic_ns"] < start["monotonic_ns"] <= p["monotonic_ns"] <= answer["monotonic_ns"] <= end["monotonic_ns"]]
        if len(exchanges) != 1:
            continue
        for sample in samples:
            if sample.get("state") == "SUCCEEDED" and exchanges[0][1]["monotonic_ns"] < sample["sample_started_ns"]:
                verified.add(sample["sample_finished_ns"])
    return verified


def required(scenario):
    """A case cannot pass qualification by silently omitting its boundary work."""
    if scenario.family == "F14":
        return {dict(concurrent="reviewers_serialized_on_binding",
                     stale="stale_review_refused_after_evidence",
                     protected_retry="reviewed_retry_after_proven_nonapplication").get(
                         scenario.review_mode, "invalid_review_mode")}
    if scenario.family == "F15":
        return {"agent_credential_rotation_collapsed", "agent_self_approval_refused", "agent_boundary_attacks_refused"}
    return set()


def _human(sample, actor, role):
    return any(p.get("principal_id") == actor and p.get("kind") == "human"
               and role in p.get("roles", []) for p in sample.get("principals", []))


def _proposal_valid(sample,proposal,operation):
    if sample.get('binding_format')!='b3-review-v1':return True
    from chaos.baselines.review_basis import digest
    native=proposal.get('native',{});basis=native.get('basis',{})
    return (native.get('operation_id')==operation and native.get('proposal_id')==proposal['proposal_id']
        and native.get('proposer_id')==proposal['proposed_by'] and native.get('action')==proposal['action']
        and basis.get('version')==proposal['bound_seq'] and basis.get('state')==proposal['bound_state']
        and digest(native)==proposal['binding_digest'])


def _native_call(call):
    if call.get('boundary_format')!='b3-review-v1':return True
    body=call.get('native_body',{})
    if call.get('event')=='proposal_displayed':
        return body.get('binding_sha256')==call.get('binding_digest') and body.get('proposal_id')==call.get('proposal_id')
    if call.get('event') in ('decision_response','decision_replayed'):
        status=body.get('status');mapping={'accepted':('outcome','approved'),'replayed':('outcome','replayed'),
            'stale':('error','stale_approval'),'already_decided':('error','already_decided')}
        if status not in mapping:return False
        key,value=mapping[status]
        return body.get('proposal_id')==call.get('proposal_id') and call.get(key)==value
    return True


def _audit_matches(sample,audit,proposal,kind,actor):
    if (audit.get('actor_id')!=actor or not str(audit.get('commit_xid','')).isdigit()
            or audit.get('details',{}).get('proposal_id')!=proposal['proposal_id']):return False
    if sample.get('binding_format')=='b3-review-v1':
        native={'decision':'decision_recorded','stale':'decision_refused_stale'}[kind]
        return (audit.get('audit_format')=='b3-review-v1' and audit.get('native_kind')==native
            and type(audit.get('native_version')) is int
            and proposal['bound_seq']<audit['native_version']<=sample['seq']
            and (kind!='decision' or audit['details'].get('approved') is True))
    return (audit.get('event_type')=={'decision':'DECISION_RECORDED','stale':'APPROVAL_REFUSED_STALE'}[kind]
        and audit['details'].get('bound_seq')==proposal['bound_seq']
        and (kind!='stale' or audit['details'].get('current_seq',-1)>proposal['bound_seq']))


def _retry_waiting(sample,proposal):
    if sample.get('binding_format')!='b3-review-v1':return proposal['bound_state']=='VERIFYING'
    from chaos.baselines.review_basis import digest
    policy=sample.get('review_policy',{})
    return (proposal['bound_state']=='UNKNOWN' and policy.get('requires_retry_review') is True
        and type(policy.get('revision')) is int and policy['revision']>0
        and proposal['native']['basis']['execution'].get('policy_sha256')==digest(policy))


def _authority_used(samples,proposal):
    native=[s for s in samples if s.get('binding_format')=='b3-review-v1']
    if not native:return True
    from chaos.baselines.review_basis import digest
    for sample in native:
        if sample.get('label')!='review_complete':continue
        grants=[g for g in sample.get('native_authorizations',[]) if g.get('proposal_id')==proposal['proposal_id']]
        uses=[u for u in sample.get('native_authorization_uses',[]) if u.get('proposal_id')==proposal['proposal_id']]
        if len(grants)!=1 or len(uses)!=1:continue
        grant,use=grants[0],uses[0]
        if grant.get('action')!=proposal['action'] or grant.get('basis_sha256')!=digest(proposal['native']['basis']['execution']):continue
        if proposal['action']=='PROTECTED_RETRY':
            if (use.get('attempt_id') and use.get('lookup_token') is None
                    and any(a.get('attempt_id')==use['attempt_id'] and a.get('ordinal')==grant.get('next_ordinal')
                            for a in sample.get('native_attempts',[]))):return True
        elif (use.get('lookup_token') and use.get('attempt_id') is None
                and any(e.get('lookup_token')==use['lookup_token'] and e.get('native_source')=='lookup'
                        and e.get('native_kind')=='applied' for e in sample.get('evidence',[]))):return True
    return False


def _review(operation, samples, calls, ledger, dispatches):
    found = set()
    for displayed in [s for s in samples if s.get("label") == "displayed_binding"]:
        for proposal in displayed.get("proposals", []):
            if not _proposal_valid(displayed,proposal,operation):continue
            pid, digest = proposal["proposal_id"], proposal["binding_digest"]
            proposer = proposal["proposed_by"]
            if not _human(displayed, proposer, "investigator"):
                continue
            screens = [c for c in calls if c.get("event") == "proposal_displayed"
                and c.get("status_code") == 200 and c.get("proposal_id") == pid
                and c.get("binding_digest") == digest and c["sample_finished_ns"] < displayed["sample_started_ns"]
                and _native_call(c)
                and c.get("actor_id") not in (proposer, displayed.get("requester_id"))
                and _human(displayed, c.get("actor_id"), "reviewer")]
            if len(screens) != 2 or len({s["actor_id"] for s in screens}) != 2:
                continue
            responses = [c for c in calls if c.get("event") == "decision_response"
                and c.get("proposal_id") == pid and c.get("binding_digest") == digest
                and c["sample_started_ns"] > displayed["sample_finished_ns"]
                and c.get("actor_id") in {s["actor_id"] for s in screens}]
            responses=[c for c in responses if _native_call(c)]
            for refused in responses:
                if refused.get("status_code") != 409 or refused.get("error") != "stale_approval":
                    continue
                changed = [s for s in samples if displayed["sample_finished_ns"] < s["sample_started_ns"]
                    and s["sample_finished_ns"] < refused["sample_started_ns"] and s["state"] == "SUCCEEDED" and s.get("lookup_confirmed")
                    and s["seq"] > proposal["bound_seq"] and s.get("evidence") != displayed.get("evidence")]
                committed = [s for s in samples if s["sample_started_ns"] > refused["sample_finished_ns"]
                    and not s.get("decisions") and any(_audit_matches(s,a,proposal,'stale',refused['actor_id'])
                        for a in s.get("audit", []))]
                if changed and committed and len(dispatches) == 1:
                    found.add("stale_review_refused_after_evidence")
            winners = [c for c in responses if c.get("status_code") == 200 and c.get("outcome") == "approved"]
            losers = [c for c in responses if c.get("status_code") == 409 and c.get("error") == "already_decided"]
            if len(winners) != 1 or len(losers) != 1 or winners[0]["actor_id"] == losers[0]["actor_id"]:
                continue
            winner, loser = winners[0], losers[0]
            if winner.get("decision_key") == loser.get("decision_key"):
                continue
            overlap = [c for c in calls if c.get("event") == "reviewers_blocked" and c.get("source") == "database"
                and c.get("operation_id") == operation and c.get("proposal_id") == pid
                and max(winner["sample_started_ns"], loser["sample_started_ns"]) < c["sample_started_ns"]
                and c["sample_finished_ns"] < min(winner["sample_finished_ns"], loser["sample_finished_ns"])
                and _blocking_chain(c)]
            replay = [c for c in calls if c.get("event") == "decision_replayed" and c.get("status_code") == 200
                and c.get("outcome") == "replayed" and c.get("proposal_id") == pid
                and c.get("actor_id") == winner["actor_id"] and c.get("decision_key") == winner.get("decision_key")
                and _native_call(c)
                and c["sample_started_ns"] > max(winner["sample_finished_ns"], loser["sample_finished_ns"])]
            committed = [s for s in samples if s.get("label") == "race_decided"
                and s["sample_started_ns"] > max(c["sample_finished_ns"] for c in replay or [winner, loser])
                and s.get("decisions") == [dict(proposal_id=pid, decided_by=winner["actor_id"],
                    decision="approve", decision_key=winner.get("decision_key"))]
                and any(_audit_matches(s,a,proposal,'decision',winner['actor_id']) for a in s.get("audit", []))]
            if not (overlap and replay and committed and _authority_used(samples,proposal)):
                continue
            if proposal["action"] == "REVERIFY" and len(dispatches) == 1:
                found.add("reviewers_serialized_on_binding")
            if (proposal["action"] == "PROTECTED_RETRY" and len(dispatches) == 2
                    and _retry_waiting(displayed,proposal)
                    and any(e.get("outcome") == "proven_not_applied" and e.get("provenance") == "system"
                            for e in displayed.get("evidence", []))
                    and any(e.get("event") == "provider_error" and e.get("error_type") == "CrmValidationRejected"
                        and e["monotonic_ns"] < displayed["sample_started_ns"] for e in ledger)
                    and dispatches[1]["monotonic_ns"] > overlap[0]["sample_finished_ns"]):
                found.add("reviewed_retry_after_proven_nonapplication")
    return found


def _blocking_chain(event):
    pids, root = event.get("api_backend_pids", []), event.get("blocker_pid")
    edges = event.get("blocking_edges", [])
    if len(pids) != 2 or len(set(pids)) != 2 or type(root) is not int or root in pids:
        return False
    reached = {root}
    for _ in edges:
        reached.update(e.get("pid") for e in edges if reached.intersection(e.get("blocked_by", [])))
    return set(pids) <= reached


ATTACKS = {
    "changed_intent": "conflicting_operation", "changed_payload": "request_conflict",
    **{"forbidden_" + k: "invalid_arguments" for k in ("tenant_id", "actor_id", "request_key", "provider_idempotency_key")},
    **{"cross_tenant_" + k: "not_found" for k in ("get_action_status", "inspect_evidence", "request_verification", "propose_resolution")},
    **{"self_approval_" + k: "unknown_tool" for k in ("approve_resolution", "decide", "safeact_api_decide", "approve")},
    "revoked_credential": "unauthenticated",
}


def _unchanged(before, after, dispatches, effects):
    fields = ("operation_ids", "decisions", "evidence", "seq", "state", "provider_dispatches", "provider_effects")
    if not all(k in before and before[k] == after.get(k) for k in fields):
        return False
    # Check the independent ledger too, not just the harness's saved counts.
    for sample in (before, after):
        n = sample["sample_started_ns"]
        if sample["provider_dispatches"] != sum(e["monotonic_ns"] <= n for e in dispatches):
            return False
        if sample["provider_effects"] != sum(e["monotonic_ns"] <= n for e in effects):
            return False
    return True


def _agent(operation, samples, calls, ledger, dispatches):
    found = set()
    final = [s for s in samples if s.get("label") == "agent_complete"]
    if len(final) != 1 or final[0].get("decisions") or len(dispatches) != 1:
        return found
    sample = final[0]
    effects = [e for e in ledger if e.get("event") == "effect"]
    principal = sample.get("requester_id")
    if not any(p.get("principal_id") == principal and p.get("kind") == "service" for p in sample.get("principals", [])):
        return found
    keys = {k["key_id"]: k for k in sample.get("agent_keys", [])}
    tenants = {p["principal_id"]: p.get("tenant_id") for p in sample.get("principals", [])}
    admitted = [c for c in calls if c.get("event") == "agent_admission" and c.get("operation_id") == operation
        and c.get("is_error") is False and keys.get(c.get("key_id"), {}).get("principal_id") == principal]
    replay = [c for c in calls if c.get("event") == "credential_replay" and c.get("operation_id") == operation
        and c.get("is_error") is False and keys.get(c.get("key_id"), {}).get("principal_id") == principal]
    if (len(admitted) >= 4 and len({c["key_id"] for c in admitted}) == 2
            and all("submit" in keys[c["key_id"]].get("scopes", []) for c in admitted)
            and any(a["key_id"] != b["key_id"]
                and max(a["sample_started_ns"], b["sample_started_ns"]) < min(a["sample_finished_ns"], b["sample_finished_ns"])
                for a in admitted for b in admitted)
            and {c["key_id"] for c in replay} == {c["key_id"] for c in admitted}
            and sample.get("operation_ids") == [operation]):
        found.add("agent_credential_rotation_collapsed")
    passed = set()
    for name, error in ATTACKS.items():
        events = [c for c in calls if c.get("event") == name and c.get("is_error") is True and c.get("error") == error]
        for event in events:
            before = [s for s in samples if s.get("label") == name + "_before"
                      and s["sample_finished_ns"] < event["sample_started_ns"]]
            after = [s for s in samples if s.get("label") == name + "_after"
                     and s["sample_started_ns"] > event["sample_finished_ns"]]
            key = keys.get(event.get("key_id"), {})
            allowed_key = (tenants.get(key.get("principal_id")) is not None
                           and tenants.get(key.get("principal_id")) != tenants.get(principal)
                           if name.startswith("cross_tenant_") else key.get("principal_id") == principal)
            if name == "revoked_credential":
                allowed_key = allowed_key and key.get("revoked") is True
            if allowed_key and any(_unchanged(b, a, dispatches, effects) for b in before for a in after):
                passed.add(name)
    human_refused = set()
    for form in ("bearer", "cookie"):
        for event in calls:
            if event.get("event") != "self_approval_human_" + form or event.get("status_code") != 401:
                continue
            before = [s for s in samples if s.get("label") == "human_endpoint_" + form + "_before"
                      and s["sample_finished_ns"] < event["sample_started_ns"]]
            after = [s for s in samples if s.get("label") == "human_endpoint_" + form + "_after"
                     and s["sample_started_ns"] > event["sample_finished_ns"]]
            if any(_unchanged(b, a, dispatches, effects) for b in before for a in after):
                human_refused.add(form)
    proposals = [c for c in calls if c.get("event") == "agent_proposal" and c.get("is_error") is False
                 and c.get("proposal_id") and c.get("operation_id") == operation]
    proposed = any(p.get("proposal_id") == event["proposal_id"] and p.get("proposed_by") == principal
                   and p.get("action") == "REVERIFY" for p in sample.get("proposals", []) for event in proposals)
    if proposed and human_refused == {"bearer", "cookie"} and all(n in passed for n in ATTACKS if n.startswith("self_approval_")):
        found.add("agent_self_approval_refused")
    if passed == set(ATTACKS):
        found.add("agent_boundary_attacks_refused")
    return found
