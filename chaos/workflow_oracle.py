"""Score fixed workflows from committed step bindings and provider effects.

The arm's restoration claim never establishes an inverse effect. The observer
must bind a separate refund to the actual charge id and authorized amount.
"""

import json

from collections import Counter

from connectors.http_trace import payload_hash


ROLES = {
    "charge": ("mock-payments", "charge_shipping_fee", "payments", "charge"),
    "order": ("mock-crm", "create_replacement_order", "crm", "create_record"),
    "compensation": ("mock-payments", "refund_payment", "payments", "refund"),
}


def wire_keys(step):
    mode=step.get('native_key_mode')
    if mode is None:return {step['correlation_sha256']}
    if mode not in ('b0','b1','b2'):raise ValueError('Unknown baseline key representation')
    from chaos.baselines.keys import payment_key
    from chaos.profiles import profile
    from connectors.http_trace import correlation_hash
    ordinals=step.get('attempt_ordinals')
    contract=profile(step['connector_id'],step['contract_version'])
    if (not isinstance(ordinals,list) or any(type(n) is not int for n in ordinals)
            or ordinals!=list(range(1,len(ordinals)+1)) or len(ordinals)>contract.max_attempts):raise ValueError('Invalid actual queue attempt ordinals')
    expected={correlation_hash(payment_key(step['operation_id'],n,mode) if contract.contract.native_idempotency else step['operation_id'])
        for n in ordinals or [1]}
    primary=correlation_hash(payment_key(step['operation_id'],ordinals[-1] if ordinals else 1,mode)
        if contract.contract.native_idempotency else step['operation_id'])
    aliases=step.get('correlation_sha256s')
    if not isinstance(aliases,list) or len(aliases)!=len(set(aliases)) or set(aliases)!=expected or step['correlation_sha256']!=primary:
        raise ValueError('Queue correlation aliases differ from actual derived wire identities')
    return expected


def intent_matches(sample, spec, requested, steps):
    """Validate the persisted representation, retaining each arm's real binding.

    B3 stores an immutable complete request rather than SafeAct's JSON plan.
    Its inverse resource is the actual charge reference; its payload and
    provider evidence still face the same effect/precondition checks below.
    """
    representation = sample.get("binding_format", "safeact-plan-v1")
    if representation == "safeact-plan-v1":
        plan = sample.get("plan", {})
        try:
            valid = (plan.get("order", {}).get("resource_id") == spec.case_id
                and json.loads(plan.get("order", {}).get("payload_canonical", "null")) ==
                    {"case_id":spec.case_id,"sku":spec.sku}
                and plan.get("compensation", {}).get("policy") == "refund_charge_when_order_failed_confirmed")
        except (ValueError, TypeError, AttributeError):
            return False
        return valid and all(s["provider_account_id"] == steps["charge"]["provider_account_id"] for s in steps.values())
    if representation not in ("b3-request-v1",'queue-request-v1','b0-request-v1'):
        return False
    request = sample.get("request")
    if not isinstance(request, dict):
        return False
    expected = dict(tenant_id=sample.get("tenant_id"), requester_id=sample.get("requester_id"),
        request_key=sample.get("request_key"),customer_id=spec.customer_id,case_id=spec.case_id,
        sku=spec.sku,fee_minor=requested.amount_minor,
        charge_contract=spec.charge_contract,order_contract=spec.order_contract,
        compensation_contract=spec.compensation_contract,
        payments_account_id=request.get("payments_account_id"),crm_account_id=request.get("crm_account_id"))
    if (request != expected or type(request.get("fee_minor")) is not int
            or any(type(expected[k]) is not str or not expected[k] for k in (
                "tenant_id","requester_id","request_key","payments_account_id","crm_account_id"))):
        return False
    return all(s.get("tenant_id") == request["tenant_id"]
        and s.get("requester_id") == request["requester_id"]
        and s.get("request_key") == f"wf:{sample['workflow_id']}:{role}"
        and s["provider_account_id"] == request["crm_account_id" if role=="order" else "payments_account_id"]
        for role,s in steps.items())


def snapshots(raw, workflow_id, *, timed=False):
    cutoff = raw.get("observation_finished_monotonic_ns", float("inf")) if timed else float("inf")
    records=raw.get('database',[]);source='database';event='db_workflow_sample';accepted=[]
    if raw.get('arm')=='b0':
        infrastructure=raw.get('infrastructure',{})
        if infrastructure.get('protocol')!='b0-process-v1' or infrastructure.get('database_created') is not False:return []
        records=infrastructure.get('process_snapshots',[]);source='process';event='process_workflow_sample'
        pids={p['pid'] for p in infrastructure.get('processes',[]) if p.get('event')=='b0_process_started'}
        accepted=[a for a in infrastructure.get('acceptance',[]) if a.get('source')=='process' and a.get('durable') is False
            and a.get('operation_id')==workflow_id and a.get('pid') in pids]
    return sorted([e for e in records
        if e.get("event") == event and e.get("source") == source
        and (source!='process' or (e.get('binding_format')=='b0-request-v1' and any(a['pid']==e.get('sender_pid')
            and a['sample_finished_ns']<e.get('sample_started_ns',0) for a in accepted)))
        and e.get("workflow_id") == workflow_id
        and type(e.get("sample_started_ns")) is int
        and type(e.get("sample_finished_ns")) is int
        and 0 < e["sample_started_ns"] <= e["sample_finished_ns"] == e.get("monotonic_ns") <= cutoff],
        key=lambda e: e["sample_finished_ns"])


def representation_matches(raw, sample):
    representation = sample.get('binding_format', 'safeact-plan-v1')
    modes = {'b0-request-v1': {'b0'}, 'queue-request-v1': {'b1', 'b2'},
             'b3-request-v1': {'b3'}}
    if representation not in modes:
        return representation == 'safeact-plan-v1' and all(
            step.get('native_key_mode') is None for step in sample.get('steps', []))
    if raw.get('arm') not in modes[representation]:
        return False
    expected = raw['arm'] if representation != 'b3-request-v1' else None
    return all(step.get('native_key_mode') == expected for step in sample.get('steps', []))


def refund_precondition(samples, charge, refund, effect_ns):
    for sample in samples:
        if sample["sample_finished_ns"] >= effect_ns:
            continue
        steps = {s["step"]: s for s in sample["steps"]}
        if (len(steps) == len(sample["steps"]) and all(name in steps for name in ROLES)
                and steps["charge"]["operation_id"] == charge["operation_id"]
                and steps["charge"]["state"] == "SUCCEEDED"
                and steps["order"]["state"] == "FAILED_CONFIRMED"
                and steps["compensation"]["operation_id"] == refund["operation_id"]
                and steps["compensation"]["compensates_operation_id"] == charge["operation_id"]):
            return True
    return False


def evaluate(scenario, raw):
    from chaos import oracle

    if len(scenario.operations) != 1 or scenario.workflow is None:
        raise ValueError("Workflow scoring requires exactly one authorized workflow")
    spec, requested = scenario.workflow, scenario.operations[0]
    errors, integrity = list(raw.get("harness_errors", [])), list(raw.get("integrity") or [])
    admissions = raw["admissions"]
    if len(admissions) != requested.submits + int(requested.changed_resubmit):
        errors.append("Workflow admission count differs from the requested submissions")
    if any(a.get("index") != 0 for a in admissions):
        errors.append("Unexpected workflow admission index")
    ids = sorted({a["local_id"] for a in admissions if a.get("local_id")})
    changed = {a["local_id"] for a in admissions if a.get("local_id") and a.get("changed")}
    effects = [(i, e) for i, e in enumerate(raw["ledger"]) if e["event"] == "effect"]
    assigned, steps_report, role_effects = set(), [], Counter()
    per_role_effects = {name: [] for name in ROLES}
    all_keys, claimed, committed, timed_committed = set(), [], [], []
    invalid_inverse = missing = 0
    cutoff = raw.get("observation_finished_monotonic_ns", float("inf"))
    for local_id in ids:
        samples = snapshots(raw, local_id)
        observed = snapshots(raw, local_id, timed=True)
        view = raw["operations"].get(local_id)
        if view is None or view.get("state") == "MISSING" or not samples:
            missing += 1
        if view is not None and view.get("state") != "MISSING":
            claimed.append(view)
        if not samples:
            errors.append("Missing independently committed workflow binding")
            continue
        if not observed:
            errors.append("Missing workflow observation inside the timed window")
        latest = samples[-1]
        if not representation_matches(raw, latest):
            integrity.append('Workflow representation or wire strategy differs from the recorded arm');continue
        steps = {s["step"]: s for s in latest["steps"]}
        if (len(steps) != len(latest["steps"]) or set(steps) - set(ROLES)
                or "charge" not in steps):
            integrity.append("Invalid workflow step set")
            continue
        operation_ids = [s["operation_id"] for s in steps.values()]
        try:key_sets=[wire_keys(s) for s in steps.values()]
        except (KeyError,ValueError,TypeError):integrity.append('Invalid native workflow wire identities');continue
        if (len(set(operation_ids)) != len(operation_ids)
                or sum(map(len,key_sets))!=len(set().union(*key_sets))):
            integrity.append("Workflow steps share operation or provider correlation identity")
            continue
        all_keys.update(set().union(*key_sets))
        # The persisted plan itself must still match the authorized request.
        expected_order = {"case_id": spec.case_id, "sku": spec.sku}
        if not intent_matches(latest, spec, requested, steps):
            integrity.append("Persisted workflow intent or account binding differs from the authorized request")
            continue
        valid = {}
        for role in ("charge", "order", "compensation"):
            step = steps.get(role)
            if step is None:
                continue
            connector, action, provider, method = ROLES[role]
            expected_payload = ({"customer_id": spec.customer_id, "amount_minor": requested.amount_minor}
                if role == "charge" else expected_order if role == "order" else None)
            if role == "compensation":
                charge_effects = per_role_effects["charge"]
                reference=steps['charge'].get('applied_reference') if latest.get('binding_format') in ('queue-request-v1','b0-request-v1') else (
                    charge_effects[0]['effect_id'] if len(charge_effects)==1 else None)
                expected_payload = {"charge_id":reference,"amount_minor":requested.amount_minor} if reference in {e['effect_id'] for e in charge_effects} else None
            bound = (step["connector_id"] == connector and step["action_type"] == action
                and step["payload"] == expected_payload
                and step.get("contract_version") == getattr(spec, f"{role}_contract")
                and (role != "compensation" or step.get("compensates_operation_id") == steps["charge"]["operation_id"]))
            if role == "charge":
                bound &= step["resource_id"] == spec.customer_id
            elif role == "order":
                bound &= step["resource_id"] == spec.case_id
            else:
                expected_resource = (expected_payload.get("charge_id") if expected_payload else None
                    ) if latest.get("binding_format") in ("b3-request-v1",'queue-request-v1','b0-request-v1') else spec.customer_id
                bound &= step["resource_id"] == expected_resource
            keys=wire_keys(step)
            calls = [e for e in raw["ledger"] if e.get("event") == "call"
                     and e.get("correlation_sha256") in keys]
            starts = [e for e in raw["ledger"] if e.get("event") == "provider_start"
                      and e.get("correlation_sha256") in keys
                      and e.get("method") in ("charge", "refund", "create_record", "send")]
            bound &= all(e.get("payload_sha256") == payload_hash(step["payload"]) for e in calls)
            bound &= all(e.get("provider") == provider and e.get("method") == method for e in starts)
            mine = [(i, e) for i, e in effects if e.get("correlation_sha256") in keys]
            if mine and (not calls or not starts):
                bound = False
            if role == "compensation":
                bound &= all(type(e.get("monotonic_ns")) is int and
                    refund_precondition(samples, steps["charge"], step, e["monotonic_ns"])
                    for _, e in mine)
            if not bound:
                integrity.append(f"Invalid {role} binding or observed provider request")
                if role == "compensation":
                    invalid_inverse += len(mine)
                continue
            if any(i in assigned for i, _ in mine):
                integrity.append("Provider effect attributed to multiple workflow steps")
                continue
            assigned.update(i for i, _ in mine)
            events = [e for _, e in mine]
            role_effects[role] += len(events)
            per_role_effects[role].extend(events)
            valid[role] = step
            metrics = oracle._provider_metrics([dict(e, resource=role) for e in oracle._observed_ledger(raw)
                if e.get("correlation_sha256") in keys]).get(role, oracle._empty_provider_metrics())
            steps_report.append(dict(workflow_id=local_id, role=role,
                operation_id=step["operation_id"], state=step["state"],
                effects=len(events), timed_effects=sum(e.get("monotonic_ns", float("inf")) <= cutoff for e in events),
                **metrics))
        committed.append(valid)
        timed_committed.append({s["step"]: s for s in observed[-1]["steps"]} if observed else {})

    classes = sorted({v["outcome_class"] for v in claimed})
    outcome = classes[0] if len(classes) == 1 else "mixed" if classes else "unaccounted" if ids else "not_accepted"
    timed_counts = {name: sum(e.get("monotonic_ns", float("inf")) <= cutoff for e in events)
                    for name, events in per_role_effects.items()}
    completed = (timed_counts == {"charge": 1, "order": 1, "compensation": 0}
        and len(committed) == len(ids) == 1
        and all(timed_committed[0].get(role, {}).get("state") == "SUCCEEDED" for role in ("charge", "order")))
    restored = (timed_counts == {"charge": 1, "order": 0, "compensation": 1}
        and len(committed) == len(ids) == 1 and "compensation" in committed[0]
        and all(timed_committed[0].get(role, {}).get("state") == state
            for role, state in (("charge", "SUCCEEDED"), ("order", "FAILED_CONFIRMED"), ("compensation", "SUCCEEDED"))))
    duplicates = sum(max(0, role_effects[name] - 1) for name in ("charge", "order"))
    inverse_duplicates = max(0, role_effects["compensation"] - 1)
    false_success = ("confirmed_success" in classes and not completed) or ("verified_compensation" in classes and not restored)
    false_failure = classes == ["confirmed_failure"] and bool(sum(role_effects.values()))
    orphans = len(effects) - len(assigned)
    starts = [e["at"] for e in raw["timeline"] if e["event"] == "submission_started" and not e.get("changed")]
    start = min(starts, default=raw["started"])
    resolved = [v["resolved_at"] for v in claimed if v.get("resolved_at") is not None]
    escalated = [v["escalated_at"] for v in claimed if v.get("escalated_at") is not None]
    calls = [e["at"] for e in raw["ledger"] if e["event"] == "call" and e.get("correlation_sha256") in all_keys]
    metrics = oracle._provider_metrics([dict(e, resource="workflow") for e in oracle._observed_ledger(raw)
        if e.get("correlation_sha256") in all_keys]).get("workflow", oracle._empty_provider_metrics())
    operation = dict(index=0, resource_id=requested.resource_id, accepted_operations=len(ids),
        changed_accepted=len(changed), effects=len(assigned), forward_effects=role_effects["charge"] + role_effects["order"],
        compensation_effects=role_effects["compensation"], prohibited_duplicates=duplicates,
        outcome_class=outcome, local_outcome_classes=classes, acceptable_terminal=tuple(requested.acceptable_terminal),
        false_success_claim=bool(false_success), false_failure_claim=bool(false_failure),
        safe_completion=bool(ids) and not missing and outcome in requested.acceptable_terminal
            and ((outcome == "confirmed_success" and completed) or (outcome == "verified_compensation" and restored)),
        resolution_seconds=oracle._since(start, max(resolved) if resolved and len(resolved) == len(ids) else None),
        escalation_seconds=oracle._since(start, min(escalated) if escalated else None),
        queue_wait_seconds=oracle._since(start, min(calls) if calls else None),
        evidence_rows=sum(v.get("evidence_rows", 0) for v in claimed), steps=steps_report, **metrics)
    result = dict(scenario_id=scenario.scenario_id, family=scenario.family, connector=scenario.connector,
        arm=raw["arm"], completion_achievable=scenario.completion_achievable, expected_operations=1,
        operations=[operation], prohibited_duplicates=duplicates, compensation_duplicates=inverse_duplicates,
        invalid_compensation_effects=invalid_inverse, orphan_effects=orphans, unaccounted=missing,
        changed_accepted=len(changed), false_claims=int(false_success) + int(false_failure),
        forward_effects=operation["forward_effects"], compensation_effects=operation["compensation_effects"],
        integrity=integrity, harness_errors=errors, cleanup_incomplete=bool(raw.get("cleanup_incomplete")),
        seconds=round(raw.get("elapsed_seconds", raw["finished"] - raw["started"]), 3))
    if oracle.safety_failures(result) or errors or result["cleanup_incomplete"]:
        operation["safe_completion"] = False
    return result
