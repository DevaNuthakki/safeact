"""Actual committed evidence and policy bindings, shared by B3 review/execution."""

import json
from hashlib import sha256

import sqlalchemy as sa


def digest(value):
    return sha256(json.dumps(value,sort_keys=True,separators=(",",":"),default=str,allow_nan=False).encode()).hexdigest()


def policy(c,t,row,reviewed):
    saved = c.execute(sa.select(t.policies).where(t.policies.c.connector_id==row["connector_id"],
        t.policies.c.contract_version==row["contract_version"])).mappings().one_or_none()
    override = (row["connector_id"],row["contract_version"]) in reviewed
    return dict(requires_retry_review=override or bool(saved and saved["requires_retry_review"]),
                revision=saved["revision"] if saved else 0,configured_override=override)


def fingerprint(c,t,row,reviewed):
    attempts = c.execute(sa.select(t.attempts).where(t.attempts.c.operation_id==row["operation_id"])
                        .order_by(t.attempts.c.ordinal)).mappings().all()
    observations = c.execute(sa.select(t.observations).where(t.observations.c.operation_id==row["operation_id"])
        .order_by(t.observations.c.created_at,t.observations.c.observation_id)).mappings().all()
    immutable = {k:row[k] for k in ("operation_id","tenant_id","requester_id","request_key","connector_id",
        "contract_version","provider_account_id","action_type","resource_id","payload_canonical","payload_sha256")}
    return dict(intent_sha256=digest(immutable),
        evidence_sha256=digest(dict(attempts=[dict(a) for a in attempts],observations=[dict(o) for o in observations])),
        policy_sha256=digest(policy(c,t,row,reviewed)),attempt_count=len(attempts))


def authorization(c,t,row,reviewed,now,action):
    basis = fingerprint(c,t,row,reviewed)
    return c.scalar(sa.select(t.authorizations.c.proposal_id).join(t.decisions).where(
        t.authorizations.c.operation_id==row["operation_id"],t.authorizations.c.action==action,
        t.authorizations.c.expires_at>now,t.authorizations.c.basis_sha256==digest(basis),
        t.authorizations.c.next_ordinal==basis["attempt_count"]+1,t.decisions.c.approved.is_(True),
        ~sa.exists(sa.select(t.authorization_uses.c.proposal_id).where(
            t.authorization_uses.c.proposal_id==t.authorizations.c.proposal_id)))
        .order_by(t.authorizations.c.expires_at,t.authorizations.c.proposal_id).limit(1))
