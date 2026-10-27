"""B3 worker/relay/reconciler over ordinary SQLAlchemy transactions.

No provider ledger or SafeAct admission/lifecycle/recovery functions are used.
Only the immutable mock profiles and connector classification are shared.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from chaos.baselines.recovery import Attempt, Fact, choose, retry_delay
from chaos.baselines.review_basis import authorization, policy
from chaos.profiles import QUAL_PROFILES
from connectors.base import DispatchKind, LookupTarget, completed_lookup
from core.contracts import EvidenceKind, LookupSemantics


@dataclass(frozen=True)
class Task:
    operation_id: UUID
    outbox_id: UUID
    delivery_id: UUID


@dataclass(frozen=True)
class Claim:
    operation_id: UUID
    token: UUID
    version: int


@dataclass(frozen=True)
class Fence:
    claim: Claim
    attempt_id: UUID
    ordinal: int
    version: int


def provider_key(operation_id):
    return "b3_" + UUID(str(operation_id)).hex


class Runtime:
    def __init__(self, engine, tables, connectors, *, lease_seconds=3,
                 profiles=QUAL_PROFILES, reviewed_contracts=(), hook=None):
        if not 0 < lease_seconds <= 300:
            raise ValueError("Bounded positive claim lease required")
        self.engine, self.t = engine, tables
        self.connectors = {c.connector_id:c for c in connectors}
        self.profiles = {(p.connector_id,p.version):p for p in profiles}
        self.reviewed = frozenset(reviewed_contracts)
        self.lease = timedelta(seconds=lease_seconds)
        self.hook = hook or (lambda point, details: None)

    @contextmanager
    def transaction(self, stage, operation_id=None):
        details = dict(operation_id=str(operation_id) if operation_id else None)
        with self.engine.begin() as connection:
            connection.info["baseline_mutated"] = False
            yield connection
            mutated = connection.info.pop("baseline_mutated",False)
            if mutated:
                details["commit_xid"] = connection.scalar(sa.text("SELECT pg_current_xact_id_if_assigned()::text"))
                if details["commit_xid"] is None:
                    raise RuntimeError("B3 write transaction has no assigned transaction identity")
                self.hook(f"before_{stage}_commit", details)
        if mutated:
            self.hook(f"after_{stage}_commit", details)

    def now(self, connection):
        return connection.scalar(sa.select(sa.func.clock_timestamp()))

    def job(self, connection, operation_id, *, skip=False):
        return connection.execute(sa.select(self.t.jobs).where(
            self.t.jobs.c.operation_id==operation_id).with_for_update(skip_locked=skip)).mappings().one_or_none()

    def profile(self, row):
        return self.profiles[row["connector_id"],row["contract_version"]]

    def intent(self, row):
        profile = self.profile(row)
        key = provider_key(row["operation_id"]) if profile.contract.native_idempotency else None
        return SimpleNamespace(**dict(row), provider_idempotency_key=key)

    def update(self, connection, row, state, reason, **changes):
        version = row["version"]+1
        values = dict(status=state,version=version,**changes)
        count = connection.execute(self.t.jobs.update().where(
            self.t.jobs.c.operation_id==row["operation_id"], self.t.jobs.c.version==row["version"],
            self.t.jobs.c.status==row["status"]).values(**values)).rowcount
        if count != 1:
            raise RuntimeError("B3 optimistic transition lost its expected version")
        connection.info["baseline_mutated"] = True
        connection.execute(self.t.audit.insert().values(operation_id=row["operation_id"],
            version=version,kind=reason,details=dict(from_state=row["status"],to_state=state)))
        return dict(row,**values)

    def history(self, connection, row):
        attempts = connection.execute(sa.select(self.t.attempts).where(
            self.t.attempts.c.operation_id==row["operation_id"]).order_by(self.t.attempts.c.ordinal)).mappings().all()
        observations = connection.execute(sa.select(self.t.observations).where(
            self.t.observations.c.operation_id==row["operation_id"]).order_by(
                self.t.observations.c.created_at,self.t.observations.c.observation_id)).mappings().all()
        facts = []
        for attempt in attempts:
            mine = [o for o in observations if o["attempt_id"]==attempt["attempt_id"] or (
                o["attempt_id"] is None and o["source"]=="lookup" and (o["kind"]=="applied"
                    or attempt["ordinal"]<=o["details"].get("through_ordinal",0)))]
            positive = [o for o in mine if o["kind"]=="applied"]
            negative = [o for o in mine if o["kind"]=="not_applied"]
            best = positive[-1] if positive else negative[-1] if negative else None
            facts.append(Attempt(attempt["ordinal"], attempt["fenced_at"],
                Fact(best["kind"]) if best else Fact.AMBIGUOUS,
                best["reference"] if best else None, best["retryable"] if best and not positive else False))
        return tuple(facts), observations

    def choice(self, connection, row, now, *, lookup_checked=False):
        facts, observations = self.history(connection,row)
        # A lookup proving non-application is job-scoped only for native keys;
        # the shared connector normalizer has checked retention and binding.
        return choose(self.profile(row),facts,now,
            investigation_started=facts[-1].fenced_at if facts else now,
            lookup_checked=lookup_checked,not_before=row["not_before"],
            review_required=self.review_required(connection,row),
            approval_valid=self.retry_authorization(connection,row,now) is not None)

    def review_required(self,c,row):
        return policy(c,self.t,row,self.reviewed)["requires_retry_review"]

    def retry_authorization(self,c,row,now):
        if not self.review_required(c,row):return None
        return authorization(c,self.t,row,self.reviewed,now,"PROTECTED_RETRY")

    def account_claim(self, connection, row, profile, now, token):
        limits = profile.limits
        if limits is None:
            return True
        account = self.t.accounts
        identity = dict(connector_id=row["connector_id"],provider_account_id=row["provider_account_id"])
        connection.execute(insert(account).values(**identity,tokens=limits.dispatch_burst or 0,
            updated_at=now).on_conflict_do_nothing())
        where = sa.and_(*(account.c[k]==v for k,v in identity.items()))
        current = connection.execute(sa.select(account).where(where).with_for_update()).mappings().one()
        if current["open_until"] and now < current["open_until"]:
            return False
        if current["probe_token"] and current["probe_until"] and now < current["probe_until"]:
            return False
        tokens = current["tokens"]
        if limits.dispatch_rate_per_second:
            tokens = min(limits.dispatch_burst,tokens+max(0,(now-current["updated_at"]).total_seconds())*limits.dispatch_rate_per_second)
            if tokens < 1:
                return False
            tokens -= 1
        probe = bool(current["open_until"])
        connection.execute(account.update().where(where).values(tokens=tokens,updated_at=now,
            probe_token=token if probe else None,
            probe_until=now+2*max(self.lease,profile.request_deadline) if probe else None))
        return True

    def account_outcome(self, connection, row, fence, result, now):
        limits = self.profile(row).limits
        if limits is None:
            return
        t = self.t.accounts
        where = sa.and_(t.c.connector_id==row["connector_id"],t.c.provider_account_id==row["provider_account_id"])
        account = connection.execute(sa.select(t).where(where).with_for_update()).mappings().one()
        failure = result.kind is DispatchKind.AMBIGUOUS or result.retryable
        failures = account["failures"]+1 if failure else 0
        values = dict(failures=failures)
        owns_probe = account["probe_token"] in (None,fence.claim.token)
        if failure and limits.breaker_threshold and failures >= limits.breaker_threshold:
            values["open_until"] = now+limits.breaker_cooldown
        elif not failure and owns_probe:
            values["open_until"] = None
        if owns_probe:
            values.update(probe_token=None,probe_until=None)
        connection.execute(t.update().where(where).values(**values))

    def claim(self, task):
        with self.transaction("claim",task.operation_id) as c:
            row = self.job(c,task.operation_id,skip=True)
            if not row or row["status"] not in ("RECORDED","RETRY_ELIGIBLE"):
                return None
            now = self.now(c)
            facts, _ = self.history(c,row)
            valid = c.scalar(sa.select(self.t.deliveries.c.delivery_id).join(self.t.outbox,
                self.t.deliveries.c.outbox_id==self.t.outbox.c.outbox_id).where(
                    self.t.deliveries.c.delivery_id==task.delivery_id,
                    self.t.deliveries.c.operation_id==task.operation_id,
                    self.t.deliveries.c.outbox_id==task.outbox_id,
                    self.t.outbox.c.operation_id==task.operation_id,self.t.outbox.c.ordinal==len(facts)))
            if valid is None or row["not_before"] and now < row["not_before"]:
                return None
            profile = self.profile(row)
            if not self.connectors[row["connector_id"]].accepts(self.intent(row)):
                raise ValueError("B3 connector does not accept this immutable intent")
            if facts and self.choice(c,row,now,lookup_checked=True).action != "retry":
                self.update(c,row,"UNKNOWN","retry_basis_changed",next_lookup_at=now)
                return None
            token = uuid4()
            if not self.account_claim(c,row,profile,now,token):
                return None
            updated = self.update(c,row,"CLAIMED","claim",claim_token=token,lease_until=now+self.lease)
            return Claim(task.operation_id,token,updated["version"])

    def fence(self, claim):
        with self.transaction("fence",claim.operation_id) as c:
            row = self.job(c,claim.operation_id)
            now = self.now(c)
            if (not row or row["status"]!="CLAIMED" or row["version"]!=claim.version
                    or row["claim_token"]!=claim.token or row["lease_until"]<=now):
                return None
            facts, _ = self.history(c,row)
            if facts and self.choice(c,row,now,lookup_checked=True).action!="retry":
                self.update(c,row,"UNKNOWN","retry_basis_expired",lease_until=None,claim_token=None,next_lookup_at=now)
                return None
            approval_id = self.retry_authorization(c,row,now) if facts else None
            attempt_id = uuid4()
            c.execute(self.t.attempts.insert().values(attempt_id=attempt_id,operation_id=claim.operation_id,
                ordinal=len(facts)+1,claim_token=claim.token,fenced_at=now,
                fenced_version=row['version']+1,lease_until=now+self.lease))
            if approval_id is not None:
                c.execute(self.t.authorization_uses.insert().values(proposal_id=approval_id,attempt_id=attempt_id))
            updated = self.update(c,row,"DISPATCHING","dispatch_fenced",lease_until=now+self.lease,
                first_fence_at=row["first_fence_at"] or now)
            return Fence(claim,attempt_id,len(facts)+1,updated["version"])

    def observation(self, connection, *, observation_id, row, source, kind, reference=None,
                    attempt_id=None, retryable=False, details=None):
        values = dict(operation_id=row["operation_id"],attempt_id=attempt_id,source=source,
            kind=kind,reference=reference,retryable=retryable,details=details or {})
        added = connection.execute(insert(self.t.observations).values(observation_id=observation_id,**values)
            .on_conflict_do_nothing().returning(self.t.observations.c.observation_id)).scalar_one_or_none()
        if added is None:
            original = connection.execute(sa.select(self.t.observations).where(
                self.t.observations.c.observation_id==observation_id)).mappings().one()
            if any(original[k]!=v for k,v in values.items()):
                raise RuntimeError("B3 observation identity conflicts with its actual result")
        else:
            connection.info["baseline_mutated"] = True
        return added is not None

    def pending_evidence(self,c,row):
        if row["status"] not in ("SUCCEEDED","FAILED_CONFIRMED"):
            c.execute(self.t.jobs.update().where(self.t.jobs.c.operation_id==row["operation_id"]).values(
                evidence_pending=True,next_lookup_at=self.now(c)))
            c.info["baseline_mutated"] = True

    def record_dispatch(self, fence, result):
        operation = fence.claim.operation_id
        with self.transaction("outcome",operation) as c:
            row = self.job(c,operation)
            attempt = c.execute(sa.select(self.t.attempts).where(
                self.t.attempts.c.attempt_id==fence.attempt_id,
                self.t.attempts.c.operation_id==operation,
                self.t.attempts.c.claim_token==fence.claim.token)).mappings().one()
            now = self.now(c)
            added = self.observation(c,observation_id=uuid5(fence.attempt_id,"dispatch"),row=row,source="dispatch",
                kind=result.kind.value,reference=result.reference,attempt_id=attempt["attempt_id"],
                retryable=result.retryable,details=dict(reason=result.reason,provider_facts=dict(result.provider_facts)))
            if not added:
                return "replayed"
            self.account_outcome(c,row,fence,result,now)
            current = (row["status"]=="DISPATCHING" and row["version"]==fence.version
                and row["claim_token"]==fence.claim.token and row["lease_until"]>now)
            if not current:
                c.execute(self.t.audit.insert().values(operation_id=operation,version=row["version"],
                    kind="late_dispatch_observed",details=dict(attempt_id=str(fence.attempt_id))))
                self.pending_evidence(c,row)
                return "stale_observed"
            retry_after = float(dict(result.provider_facts).get("retry_after",0))
            delay = retry_delay(self.profile(row),row["request_key"],fence.ordinal,retry_after=retry_after)
            updated = self.update(c,row,"UNKNOWN","dispatch_observed",claim_token=None,lease_until=None,
                not_before=now+delay,next_lookup_at=now+delay if result.kind is DispatchKind.NOT_APPLIED else now)
            self.apply_choice(c,updated,self.choice(c,updated,now),now)
            return "recorded"

    def apply_choice(self, c, row, choice, now):
        if choice.action=="complete":
            if row["status"]=="ESCALATED_UNRESOLVED" and row["reopens"]>=self.profile(row).max_reopens:
                return row
            return self.update(c,row,"SUCCEEDED",choice.reason,resolved_at=now,lookup_token=None,
                lookup_lease_until=None,reopens=row["reopens"]+int(row["status"]=="ESCALATED_UNRESOLVED"))
        if choice.action=="fail":
            return self.update(c,row,"FAILED_CONFIRMED",choice.reason,resolved_at=now,
                lookup_token=None,lookup_lease_until=None)
        if choice.action=="retry":
            facts, _ = self.history(c,row)
            c.execute(insert(self.t.outbox).values(outbox_id=uuid4(),operation_id=row["operation_id"],
                ordinal=len(facts)).on_conflict_do_nothing())
            return self.update(c,row,"RETRY_ELIGIBLE",choice.reason,lookup_token=None,lookup_lease_until=None)
        if choice.action=="escalate" and row["status"]!="ESCALATED_UNRESOLVED":
            return self.update(c,row,"ESCALATED_UNRESOLVED",choice.reason,escalated_at=now,
                lookup_token=None,lookup_lease_until=None,next_lookup_at=now+self.profile(row).post_escalation_check_interval)
        return row

    def execute(self, task):
        claim = self.claim(task)
        if claim is None:
            return "not_claimable"
        fence = self.fence(claim)
        if fence is None:
            return "not_fenced"
        with self.engine.connect() as c:
            row = c.execute(sa.select(self.t.jobs).where(self.t.jobs.c.operation_id==task.operation_id)).mappings().one()
            fenced_at = c.scalar(sa.select(self.t.attempts.c.fenced_at).where(self.t.attempts.c.attempt_id==fence.attempt_id))
            remaining = (fenced_at+self.profile(row).request_deadline-self.now(c)).total_seconds()
        if remaining<=0:
            return "request_deadline_elapsed"
        self.hook("before_provider_call",dict(operation_id=str(task.operation_id),attempt_id=str(fence.attempt_id)))
        with self.engine.connect() as c:
            remaining = (fenced_at+self.profile(row).request_deadline-self.now(c)).total_seconds()
        if remaining<=0:
            return "request_deadline_elapsed"
        result = self.connectors[row["connector_id"]].dispatch(self.intent(row),operation_id=task.operation_id,timeout=remaining)
        self.hook("after_provider_call",dict(operation_id=str(task.operation_id),attempt_id=str(fence.attempt_id)))
        return self.record_dispatch(fence,result)

    def relay_once(self, publish, *, limit=100):
        tasks = []
        with self.transaction("publication") as c:
            now = self.now(c)
            rows = c.execute(sa.select(self.t.outbox).join(self.t.jobs).where(
                self.t.jobs.c.status.in_(("RECORDED","RETRY_ELIGIBLE")),
                sa.or_(self.t.jobs.c.not_before.is_(None),self.t.jobs.c.not_before<=now),
                sa.or_(self.t.outbox.c.published_at.is_(None),self.t.outbox.c.published_at<=now-timedelta(seconds=1)),
                sa.or_(self.t.outbox.c.publication_lease_until.is_(None),self.t.outbox.c.publication_lease_until<=now)
            ).order_by(self.t.outbox.c.created_at).limit(limit).with_for_update(skip_locked=True,of=self.t.outbox)).mappings().all()
            for row in rows:
                # Publish only the current attempt generation, never old outboxes.
                count = c.scalar(sa.select(sa.func.count()).select_from(self.t.attempts).where(
                    self.t.attempts.c.operation_id==row["operation_id"]))
                if row["ordinal"]!=count:
                    continue
                token = uuid4()
                c.execute(self.t.deliveries.insert().values(delivery_id=token,outbox_id=row["outbox_id"],operation_id=row["operation_id"]))
                c.info["baseline_mutated"] = True
                c.execute(self.t.outbox.update().where(self.t.outbox.c.outbox_id==row["outbox_id"]).values(
                    publication_token=token,publication_lease_until=now+timedelta(seconds=2)))
                tasks.append(Task(row["operation_id"],row["outbox_id"],token))
        for task in tasks:
            publish(task)  # Broker I/O outside any PostgreSQL transaction.
            with self.transaction("publication_ack",task.operation_id) as c:
                changed = c.execute(self.t.outbox.update().where(self.t.outbox.c.outbox_id==task.outbox_id,
                    self.t.outbox.c.publication_token==task.delivery_id).values(
                        published_at=self.now(c),publication_token=None,publication_lease_until=None))
                c.info["baseline_mutated"] = changed.rowcount==1
        return len(tasks)

    def reserve_lookup(self, operation):
        with self.transaction("investigation",operation) as c:
            row = self.job(c,operation,skip=True)
            if not row or row["status"] in ("SUCCEEDED","FAILED_CONFIRMED"):
                return None
            now = self.now(c)
            if row["status"] in ("RECORDED","RETRY_ELIGIBLE"):
                if not row["evidence_pending"]:return None
                row = self.update(c,row,"UNKNOWN","ready_evidence_changed",evidence_pending=False,next_lookup_at=now)
            if row["status"]=="CLAIMED":
                if row["lease_until"]>now:
                    return None
                count = row["abandoned_claims"]+1
                self.update(c,row,"RECORDED" if count<3 else "ESCALATED_UNRESOLVED","claim_expired",claim_token=None,
                    lease_until=None,abandoned_claims=count,escalated_at=now if count>=3 else row["escalated_at"])
                return None
            if row["status"]=="DISPATCHING":
                if row["lease_until"]>now:
                    return None
                row = self.update(c,row,"UNKNOWN","dispatch_lease_expired",claim_token=None,lease_until=None,next_lookup_at=now)
            if row["lookup_token"] and row["lookup_lease_until"]>now:
                return None
            if row["status"]=="VERIFYING":
                row = self.update(c,row,"ESCALATED_UNRESOLVED" if row["escalated_at"] else "UNKNOWN","lookup_lease_expired",
                    lookup_token=None,lookup_lease_until=None)
            if row["evidence_pending"]:
                row = self.update(c,row,row["status"],"new_evidence_seen",evidence_pending=False)
            facts, _ = self.history(c,row)
            if not facts:
                return None
            choice = self.choice(c,row,now)
            row = self.apply_choice(c,row,choice,now)
            if row["status"] in ("RETRY_ELIGIBLE","SUCCEEDED","FAILED_CONFIRMED"):
                return None
            profile = self.profile(row)
            if choice.action=="review" or choice.reason=="retry_backoff":
                due = facts[-1].fenced_at+profile.investigation_window if choice.action=="review" else row["not_before"]
                if due!=row["next_lookup_at"]:
                    self.update(c,row,row["status"],"recovery_wait_scheduled",next_lookup_at=due)
                return None
            if profile.contract.lookup_semantics is LookupSemantics.UNAVAILABLE:
                return None
            reverify = authorization(c,self.t,row,self.reviewed,now,"REVERIFY") if row["escalated_at"] else None
            if row["escalated_at"] and now>=row["escalated_at"]+profile.post_escalation_horizon and reverify is None:
                if row["next_lookup_at"] is not None:
                    self.update(c,row,row["status"],"post_escalation_checks_exhausted",next_lookup_at=None)
                return None
            if row["next_lookup_at"] and now<row["next_lookup_at"] and reverify is None:
                return None
            token = uuid4()
            count = row["lookup_count"]
            interval = (min(profile.limits.lookup_interval*2**min(count,20),profile.limits.lookup_interval_cap)
                        if profile.limits else profile.post_escalation_check_interval)
            if row["escalated_at"]:
                interval = profile.post_escalation_check_interval
            updated = self.update(c,row,"VERIFYING","lookup_reserved",lookup_token=token,
                lookup_lease_until=now+timedelta(seconds=6),lookup_count=count+1,next_lookup_at=now+interval)
            if reverify is not None:
                c.execute(self.t.authorization_uses.insert().values(proposal_id=reverify,lookup_token=token))
            return dict(updated,lookup_through_ordinal=len(facts)),token

    def reconcile(self, operation):
        reserved = self.reserve_lookup(operation)
        if reserved is None:
            return "not_due"
        row,token = reserved
        profile = self.profile(row)
        with self.engine.connect() as c:
            observed_at = self.now(c)
        target = LookupTarget(
            row["operation_id"],row["provider_account_id"],self.intent(row).provider_idempotency_key,
            row["payload_sha256"],row["first_fence_at"],observed_at)
        result = self.connectors[row["connector_id"]].lookup(target,profile)
        with self.transaction("lookup",operation) as c:
            current = self.job(c,operation)
            now = self.now(c)
            result = completed_lookup(profile,target,result,now)
            kind = "not_applied" if result.kind is EvidenceKind.PROVEN_NOT_APPLIED else result.kind.value
            reference = result.reference
            note = result.note
            self.observation(c,observation_id=token,row=current,source="lookup",kind=kind,
                reference=reference,retryable=kind=="not_applied",details=dict(note=note,
                    through_ordinal=row["lookup_through_ordinal"]))
            if (current["status"]!="VERIFYING" or current["lookup_token"]!=token
                    or current["version"]!=row["version"] or current["lookup_lease_until"]<=now):
                self.pending_evidence(c,current)
                c.execute(self.t.audit.insert().values(operation_id=operation,version=current["version"],
                    kind="late_lookup_observed",details=dict(lookup_token=str(token))))
                return "stale_observed"
            current = self.update(c,current,"ESCALATED_UNRESOLVED" if current["escalated_at"] else "UNKNOWN",
                "lookup_observed",lookup_token=None,lookup_lease_until=None)
            self.apply_choice(c,current,self.choice(c,current,now,lookup_checked=True),now)
            return "recorded"

    def reconcile_once(self, *, limit=100):
        with self.engine.connect() as c:
            now = self.now(c)
            j = self.t.jobs.c
            due = sa.or_(j.next_lookup_at.is_(None),j.next_lookup_at<=now)
            ids = c.scalars(sa.select(self.t.jobs.c.operation_id).where(
                sa.or_(sa.and_(j.status.in_(("CLAIMED","DISPATCHING")),j.lease_until<=now),
                    sa.and_(j.status=="VERIFYING",j.lookup_lease_until<=now),
                    sa.and_(j.status=="UNKNOWN",sa.or_(due,j.evidence_pending)),
                    sa.and_(j.status.in_(("RECORDED","RETRY_ELIGIBLE")),j.evidence_pending),
                    sa.and_(j.status=="ESCALATED_UNRESOLVED",sa.or_(j.evidence_pending,
                        sa.and_(j.next_lookup_at.is_not(None),j.next_lookup_at<=now)))))
                .order_by(self.t.jobs.c.created_at).limit(limit)).all()
        return {operation:self.reconcile(operation) for operation in ids}
