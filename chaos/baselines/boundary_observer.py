"""Read-only native B3 human-review records; no credentials or sessions."""

import time
from uuid import UUID

import sqlalchemy as sa

from chaos.baselines.observer import Observer
from chaos.baselines.review_basis import digest
from chaos.database_trace import _interval
from connectors.http_trace import correlation_hash


FORMAT='b3-review-v1'


def sample(engine,t,operation_id,label):
    op=UUID(str(operation_id));started=time.monotonic_ns()
    with Observer(engine,t).snapshot() as c:
        row=c.execute(sa.select(t.jobs).where(t.jobs.c.operation_id==op)).mappings().one()
        proposals=c.execute(sa.select(t.proposals).where(t.proposals.c.operation_id==op)
            .order_by(t.proposals.c.proposal_id)).mappings().all()
        decisions=c.execute(sa.select(t.decisions).join(t.proposals).where(t.proposals.c.operation_id==op)
            .order_by(t.decisions.c.proposal_id)).mappings().all()
        evidence=c.execute(sa.select(t.observations).where(t.observations.c.operation_id==op)
            .order_by(t.observations.c.observation_id)).mappings().all()
        audits=c.execute(sa.select(t.audit).where(t.audit.c.operation_id==op).order_by(t.audit.c.audit_id)).mappings().all()
        humans=c.execute(sa.select(t.humans.c.actor_id,t.humans.c.tenant_id,t.humans.c.roles).where(
            t.humans.c.enabled.is_(True)).order_by(t.humans.c.actor_id)).mappings().all()
        operations=c.scalars(sa.select(t.jobs.c.operation_id).where(t.jobs.c.resource_id==row['resource_id'],
            t.jobs.c.tenant_id==row['tenant_id']).order_by(t.jobs.c.operation_id)).all()
        saved_policy=c.execute(sa.select(t.policies).where(t.policies.c.connector_id==row['connector_id'],
            t.policies.c.contract_version==row['contract_version'])).mappings().one_or_none()
        grants=c.execute(sa.select(t.authorizations).where(t.authorizations.c.operation_id==op)).mappings().all()
        uses=c.execute(sa.select(t.authorization_uses).join(t.authorizations).where(t.authorizations.c.operation_id==op)).mappings().all()
        attempts=c.execute(sa.select(t.attempts.c.attempt_id,t.attempts.c.ordinal).where(t.attempts.c.operation_id==op)).mappings().all()
    normalized=[]
    for p in proposals:
        native=dict(proposal_id=str(p['proposal_id']),operation_id=str(p['operation_id']),proposer_id=p['proposer_id'],
            action=p['action'],basis=p['basis'],expires_at=str(p['expires_at']))
        if digest(native)!=p['binding_sha256']:raise RuntimeError('B3 persisted proposal binding is inconsistent')
        normalized.append(dict(proposal_id=str(p['proposal_id']),proposed_by=p['proposer_id'],action=p['action'],
            bound_seq=p['basis']['version'],bound_state=p['basis']['state'],binding_digest=p['binding_sha256'],native=native))
    return dict(_interval(started,time.monotonic_ns()),event='db_boundary_snapshot',binding_format=FORMAT,label=label,
        operation_id=str(op),state=row['status'],seq=row['version'],requester_id=row['requester_id'],
        provider=row['connector_id'].removeprefix('mock-'),correlation_sha256=correlation_hash(str(op)),
        operation_ids=list(map(str,operations)),
        review_policy=dict(requires_retry_review=bool(saved_policy and saved_policy['requires_retry_review']),
            revision=saved_policy['revision'] if saved_policy else 0,configured_override=False),
        native_authorizations=[dict(proposal_id=str(g['proposal_id']),action=g['action'],next_ordinal=g['next_ordinal'],
            basis_sha256=g['basis_sha256']) for g in grants],
        native_authorization_uses=[dict(proposal_id=str(u['proposal_id']),attempt_id=str(u['attempt_id']) if u['attempt_id'] else None,
            lookup_token=str(u['lookup_token']) if u['lookup_token'] else None) for u in uses],
        native_attempts=[dict(attempt_id=str(a['attempt_id']),ordinal=a['ordinal']) for a in attempts],
        principals=[dict(principal_id=h['actor_id'],tenant_id=h['tenant_id'],roles=h['roles'],kind='human') for h in humans],
        proposals=normalized,decisions=[dict(proposal_id=str(d['proposal_id']),decided_by=d['reviewer_id'],
            decision='approve' if d['approved'] else 'reject',decision_key=d['decision_key']) for d in decisions],
        evidence=[dict(outcome_id=str(e['observation_id']),native_kind=e['kind'],native_source=e['source'],
            reference=e['reference'],retryable=e['retryable'],lookup_token=str(e['observation_id']) if e['source']=='lookup' else None,
            outcome={'not_applied':'proven_not_applied','applied':'confirmed_applied'}.get(e['kind'],e['kind']),
            evidence_kind=e['kind'],provenance='system') for e in evidence],
        audit=[dict(audit_id=a['audit_id'],audit_format=FORMAT,native_kind=a['kind'],native_version=a['version'],
            actor_id=a['details'].get('actor_id'),commit_xid=a['commit_xid'],details=a['details']) for a in audits])
