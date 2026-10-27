"""Feature applicability, declared independently of observed outcomes."""


def reason(arm,scenario):
    if scenario.family in arm.not_applicable:
        return {'F14':'No authenticated human-review surface','F15':'No guarded agent credential surface'}.get(
            scenario.family,'Declared absent feature: '+scenario.family)
    features=getattr(arm,'capabilities',None)
    if features is None:return None
    needed={}
    families=set(scenario.family.split('+'))
    if 'F06' in families:needed['retry_deadline']='No persisted execution/retry deadline'
    if 'F11' in families:needed['durable_transactions']='No durable transaction acknowledgement boundary'
    if 'F12' in families:needed['lookup']='No provider lookup to receive the invalid/unavailable lookup response'
    for fault in scenario.faults:
        kind=fault['kind']
        if kind in ('broker_loss','broker_kill','broker_redelivery'):needed['broker']='No broker or queued transport'
        if kind in ('hold_after_empty_lookup','workflow_lookup'):needed['lookup']='No provider lookup to receive the selected lookup fault'
        if kind=='hold_after_escalation':needed['escalation']='No manual-escalation feature to trigger the late-commit interleaving'
    missing=[description for feature,description in sorted(needed.items()) if feature not in features]
    return '; '.join(missing) if missing else None


def critical_labels(arm):
    """Absent mechanisms only; weak or failed guards remain measurable results."""
    from chaos.coverage import CRITICAL

    features = getattr(arm, 'capabilities', None)
    if features is None:
        features = {'broker', 'durable_transactions', 'retry_deadline', 'lookup',
                    'escalation', 'human_review', 'agent', 'provider_breaker'}
    else:
        features = set(features)
    if 'F14' in arm.not_applicable:
        features.discard('human_review')
    if 'F15' in arm.not_applicable:
        features.discard('agent')
    needed = {
        'reviewers_serialized_on_binding': 'human_review',
        'stale_review_refused_after_evidence': 'human_review',
        'reviewed_retry_after_proven_nonapplication': 'human_review',
        'agent_credential_rotation_collapsed': 'agent',
        'agent_self_approval_refused': 'agent',
        'agent_boundary_attacks_refused': 'agent',
        'late_commit_after_empty_lookup': 'lookup',
        'lookup_lied_outside_contract': 'lookup',
        'compensation_verified_by_lookup': 'lookup',
        'late_commit_after_escalation': 'escalation',
        'lease_expired_while_request_ran': 'retry_deadline',
        'provider_outage_spanned_deadline': 'retry_deadline',
        'breaker_open_with_unresolved_work': 'provider_breaker',
        'retention_expired_before_outcome': 'durable_transactions',
        'commit_ack_lost': 'durable_transactions',
        'broker_messages_lost': 'broker',
        'broker_process_killed': 'broker',
        'broker_redelivered_during_dispatch': 'broker',
    }
    return {label for label in CRITICAL if label not in needed or needed[label] in features}
