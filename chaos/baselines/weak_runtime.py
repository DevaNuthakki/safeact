"""B1/B2 queue retries: no connector lookup, outcome fencing or B3 choices."""

from types import SimpleNamespace
from uuid import uuid4,uuid5

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from chaos.baselines.keys import payment_key
from chaos.baselines.recovery import retry_delay
from chaos.baselines.runtime import Claim,Fence,Runtime
from connectors.base import DispatchKind


class QueueRuntime(Runtime):
    def __init__(self,*args,mode,**kwargs):
        if mode not in ('b1','b2'):raise ValueError('Queue baseline mode required')
        super().__init__(*args,**kwargs);self.mode=mode

    def intent(self,row):
        native=self.profile(row).contract.native_idempotency
        key=payment_key(row['operation_id'],row.get('queue_ordinal',1),self.mode) if native else None
        return SimpleNamespace(**dict(row),provider_idempotency_key=key)

    def execute(self,task):
        with self.transaction('delivery',task.operation_id) as c:
            row=self.job(c,task.operation_id)
            if row is None or row['status'] in ('SUCCEEDED','FAILED_CONFIRMED'):return 'not_claimable'
            valid=c.scalar(sa.select(self.t.deliveries.c.delivery_id).where(
                self.t.deliveries.c.operation_id==task.operation_id,self.t.deliveries.c.outbox_id==task.outbox_id,
                self.t.deliveries.c.delivery_id==task.delivery_id))
            if valid is None:return 'not_claimable'
            facts,_=self.history(c,row);now=self.now(c);profile=self.profile(row)
            if len(facts)>=profile.max_attempts or (row['first_fence_at'] and now>=row['first_fence_at']+profile.investigation_window):
                self.update(c,row,'UNKNOWN','queue_retry_window_closed',claim_token=None,lease_until=None)
                return 'not_claimable'
            if row['not_before'] and now<row['not_before']:return 'not_claimable'
            token,attempt=uuid4(),uuid4()
            if not self.account_claim(c,row,profile,now,token):return 'not_claimable'
            ordinal=len(facts)+1
            c.execute(self.t.attempts.insert().values(attempt_id=attempt,operation_id=task.operation_id,ordinal=ordinal,
                claim_token=token,fenced_at=now,fenced_version=row['version']+1,lease_until=now+self.lease))
            updated=self.update(c,row,'DISPATCHING','queue_delivery_started',claim_token=token,lease_until=now+self.lease,
                first_fence_at=row['first_fence_at'] or now)
        # A second delivery can reach here while the original call is running.
        # The version/token are recorded metadata, not ownership requirements.
        row=dict(updated,queue_ordinal=ordinal)
        details=dict(operation_id=str(task.operation_id),attempt_id=str(attempt))
        self.hook('before_provider_call',details)
        result=self.connectors[row['connector_id']].dispatch(self.intent(row),operation_id=task.operation_id,
            timeout=profile.request_deadline.total_seconds())
        self.hook('after_provider_call',details)
        with self.transaction('outcome',task.operation_id) as c:
            current=self.job(c,task.operation_id);now=self.now(c)
            self.observation(c,observation_id=uuid5(attempt,'dispatch'),row=current,source='dispatch',kind=result.kind.value,
                reference=result.reference,attempt_id=attempt,retryable=result.retryable,
                details=dict(reason=result.reason,provider_facts=dict(result.provider_facts)))
            self.account_outcome(c,current,Fence(Claim(task.operation_id,token,updated['version']),attempt,ordinal,updated['version']),result,now)
            if result.kind is DispatchKind.APPLIED:
                c.execute(insert(self.t.queue_results).values(operation_id=task.operation_id,attempt_id=attempt,
                    reference=result.reference).on_conflict_do_nothing())
                if current['status']!='SUCCEEDED':self.update(c,current,'SUCCEEDED','queue_positive_response',claim_token=None,lease_until=None,resolved_at=now)
            elif current['status']!='SUCCEEDED':self._retry_or_wait(c,current,now,result=result)
        return 'recorded'

    def _retry_or_wait(self,c,row,now,*,result=None):
        facts,_=self.history(c,row);profile=self.profile(row)
        all_rejected=all(f.fact.value=='not_applied' for f in facts)
        if all_rejected and result is not None and not result.retryable:
            return self.update(c,row,'FAILED_CONFIRMED','queue_permanent_rejection',claim_token=None,lease_until=None,resolved_at=now)
        if len(facts)>=profile.max_attempts or now>=row['first_fence_at']+profile.investigation_window:
            return self.update(c,row,'UNKNOWN','queue_retry_budget_exhausted',claim_token=None,lease_until=None)
        retry_after=float(dict(result.provider_facts).get('retry_after',0)) if result is not None else 0
        delay=retry_delay(profile,row['request_key'],len(facts),retry_after=retry_after)
        c.execute(insert(self.t.outbox).values(outbox_id=uuid4(),operation_id=row['operation_id'],ordinal=len(facts)).on_conflict_do_nothing())
        return self.update(c,row,'RETRY_ELIGIBLE','queue_retry_scheduled',claim_token=None,lease_until=None,not_before=now+delay)

    def reconcile_once(self,*,limit=100):
        # This is only a queue visibility timeout/retry scan. It never looks up effects.
        with self.engine.connect() as c:
            ids=c.scalars(sa.select(self.t.jobs.c.operation_id).where(self.t.jobs.c.status=='DISPATCHING',
                self.t.jobs.c.lease_until<=sa.func.clock_timestamp()).limit(limit)).all()
        result={}
        for op in ids:
            with self.transaction('outcome',op) as c:
                row=self.job(c,op);now=self.now(c)
                if row['status']=='DISPATCHING' and row['lease_until']<=now:
                    self._retry_or_wait(c,row,now);result[op]='queue_timeout'
        return result


from chaos.baselines.workflow import CorruptWorkflow,Workflow


class QueueWorkflow(Workflow):
    def applied_reference(self,c,row):
        result=c.execute(sa.select(self.t.queue_results).where(self.t.queue_results.c.operation_id==row['operation_id'])).mappings().one_or_none()
        if result is None:raise CorruptWorkflow('Queue workflow has no recorded successful response')
        proof=c.scalar(sa.select(self.t.observations.c.observation_id).where(
            self.t.observations.c.operation_id==row['operation_id'],self.t.observations.c.attempt_id==result['attempt_id'],
            self.t.observations.c.kind=='applied',self.t.observations.c.reference==result['reference']))
        if proof is None:raise CorruptWorkflow('Queue workflow response lacks its actual dispatch observation')
        return result['reference']
