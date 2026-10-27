"""Conventional, authenticated version-bound B3 human review transactions."""

import hmac
import re
from datetime import timedelta
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from chaos.baselines.identity import Forbidden, Identity, Missing
from chaos.baselines.recovery import choose
from chaos.baselines.review_basis import digest, fingerprint
from core.contracts import LookupSemantics


class Review:
    def __init__(self,runtime):
        self.runtime,self.t,self.engine = runtime,runtime.t,runtime.engine
        self.identity = Identity(self.engine,self.t)

    def job(self,c,operation_id,human):
        row = self.runtime.job(c,operation_id)
        if row is None or row["tenant_id"]!=human["tenant_id"]:
            raise Missing("Action not found")
        return row

    def subject(self,c,token,operation_id,roles):
        human = self.identity.authenticate(c,token,roles=roles)
        row = self.job(c,operation_id,human)
        # A request may have waited on the operation lock. Check the live
        # session/role/tenant again at the point where it can actually act.
        human = self.identity.authenticate(c,token,roles=roles)
        if row["tenant_id"]!=human["tenant_id"]:raise Missing("Action not found")
        return human,row

    def basis(self,c,row):
        return dict(version=row["version"],state=row["status"],
                    execution=fingerprint(c,self.t,row,self.runtime.reviewed))

    def audit(self,c,row,kind,human,**details):
        c.execute(self.t.audit.insert().values(operation_id=row["operation_id"],version=row["version"],kind=kind,
            details=dict(actor_id=human["actor_id"],**details)))
        c.info["baseline_mutated"] = True

    def eligible(self,c,row,action,now):
        facts,_ = self.runtime.history(c,row)
        if not facts:return False
        if action=="REVERIFY":
            return (row["status"]=="ESCALATED_UNRESOLVED" and row["reopens"]<self.runtime.profile(row).max_reopens
                    and self.runtime.profile(row).contract.lookup_semantics is not LookupSemantics.UNAVAILABLE)
        if action=="PROTECTED_RETRY" and row["status"] in ("UNKNOWN","VERIFYING"):
            return choose(self.runtime.profile(row),facts,now,investigation_started=facts[-1].fenced_at,
                lookup_checked=True,review_required=True,approval_valid=True).action=="retry"
        return False

    def snapshot(self,token,operation_id):
        with self.engine.begin() as c:
            human,row = self.subject(c,token,operation_id,("investigator","reviewer"))
            return dict(operation_id=operation_id,**self.basis(c,row))

    def proposal(self,token,operation_id,proposal_id):
        with self.engine.begin() as c:
            self.subject(c,token,operation_id,('investigator','reviewer'))
            saved=c.execute(sa.select(self.t.proposals).where(self.t.proposals.c.operation_id==operation_id,
                self.t.proposals.c.proposal_id==proposal_id)).mappings().one_or_none()
            if saved is None:raise Missing('Proposal not found')
            return {k:saved[k] for k in ('proposal_id','operation_id','proposer_id','action','basis','binding_sha256','expires_at')}

    def propose(self,token,operation_id,action,*,ttl_seconds=120):
        if action not in ("PROTECTED_RETRY","REVERIFY") or type(ttl_seconds) not in (int,float) or not 0<ttl_seconds<=600:
            raise ValueError("An allowed review action and bounded expiry are required")
        with self.runtime.transaction("proposal",operation_id) as c:
            human,row = self.subject(c,token,operation_id,("investigator",))
            now = self.runtime.now(c)
            if not self.eligible(c,row,action,now):
                self.audit(c,row,"proposal_refused",human,action=action)
                return dict(status="ineligible")
            proposal_id,expires = uuid4(),now+timedelta(seconds=ttl_seconds)
            basis = self.basis(c,row)
            binding = digest(dict(proposal_id=proposal_id,operation_id=operation_id,proposer_id=human["actor_id"],
                                  action=action,basis=basis,expires_at=expires))
            c.execute(self.t.proposals.insert().values(proposal_id=proposal_id,operation_id=operation_id,
                proposer_id=human["actor_id"],action=action,basis=basis,binding_sha256=binding,expires_at=expires))
            self.audit(c,row,"proposal_created",human,proposal_id=str(proposal_id),action=action,binding_sha256=binding)
            return dict(status="proposed",proposal_id=proposal_id,binding_sha256=binding,action=action,
                        basis=basis,expires_at=expires)

    def decide(self,token,operation_id,proposal_id,binding,decision_key,approved):
        if (type(approved) is not bool or type(decision_key) is not str or not 1<=len(decision_key)<=128
                or any(ch.isspace() or ord(ch)<32 for ch in decision_key) or type(binding) is not str
                or re.fullmatch(r"[0-9a-f]{64}",binding) is None):
            raise ValueError("An exact displayed binding and stable decision identity are required")
        with self.runtime.transaction("decision",operation_id) as c:
            human,row = self.subject(c,token,operation_id,("reviewer",))
            proposal = c.execute(sa.select(self.t.proposals).where(self.t.proposals.c.proposal_id==proposal_id,
                self.t.proposals.c.operation_id==operation_id)).mappings().one_or_none()
            if proposal is None:raise Missing("Proposal not found")
            if human["actor_id"] in (row["requester_id"],proposal["proposer_id"]):
                raise Forbidden("Requester, proposer and reviewer must be independent")
            existing = c.execute(sa.select(self.t.decisions).where(
                self.t.decisions.c.proposal_id==proposal_id)).mappings().one_or_none()
            exact_binding = hmac.compare_digest(binding,proposal["binding_sha256"])
            if existing:
                replay = (existing["reviewer_id"]==human["actor_id"] and existing["decision_key"]==decision_key
                          and existing["approved"]==approved and exact_binding)
                status = "replayed" if replay else "already_decided"
                self.audit(c,row,"decision_"+status,human,proposal_id=str(proposal_id))
                return dict(status=status,proposal_id=proposal_id)
            now = self.runtime.now(c)
            if (not exact_binding or proposal["expires_at"]<=now or self.basis(c,row)!=proposal["basis"]
                    or not self.eligible(c,row,proposal["action"],now)):
                self.audit(c,row,"decision_refused_stale",human,proposal_id=str(proposal_id))
                return dict(status="stale",proposal_id=proposal_id)
            added = c.execute(insert(self.t.decisions).values(proposal_id=proposal_id,reviewer_id=human["actor_id"],
                decision_key=decision_key,approved=approved).on_conflict_do_nothing()
                .returning(self.t.decisions.c.proposal_id)).scalar_one_or_none()
            if added is None:
                self.audit(c,row,"decision_key_conflict",human,proposal_id=str(proposal_id))
                return dict(status="key_conflict",proposal_id=proposal_id)
            if approved:
                execution = proposal["basis"]["execution"]
                c.execute(self.t.authorizations.insert().values(proposal_id=proposal_id,operation_id=operation_id,
                    action=proposal["action"],basis_sha256=digest(execution),
                    next_ordinal=execution["attempt_count"]+1,expires_at=proposal["expires_at"]))
                row = self.runtime.update(c,row,row["status"],"human_action_authorized",next_lookup_at=now,
                                          evidence_pending=proposal["action"]=="REVERIFY")
                if proposal["action"]=="PROTECTED_RETRY":
                    row = self.runtime.apply_choice(c,row,self.runtime.choice(c,row,now,lookup_checked=True),now)
            self.audit(c,row,"decision_recorded",human,proposal_id=str(proposal_id),approved=approved)
            return dict(status="accepted",proposal_id=proposal_id)
