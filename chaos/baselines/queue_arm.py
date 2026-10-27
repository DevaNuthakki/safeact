"""B1/B2 adapters with explicit queue-only execution and capability limits."""

import time
from uuid import UUID

import sqlalchemy as sa

from chaos.baselines.arm import B3Arm
from chaos.baselines.keys import payment_key
from chaos.broker_redelivery import DispatchRedelivery,restore_unacked
from chaos.database_trace import lease_rows


class QueueArm(B3Arm):
    not_applicable=frozenset({'F14','F15'})
    capabilities=frozenset({'broker','durable_transactions','retry_deadline','killable_worker','fixed_workflow','provider_breaker'})
    point_map={'after_claim_commit':'after_delivery_commit','after_fence_commit':'after_delivery_commit'}

    def arm_failpoint(self,point,action):super().arm_failpoint(self.point_map.get(point,point),action)
    def kill_parked_worker(self,point,emit):super().kill_parked_worker(self.point_map.get(point,point),emit)

    def infrastructure_observations(self,since_ns):
        result=super().infrastructure_observations(since_ns)
        result.update(execution_mode=self.execution_mode,point_map=self.point_map,capabilities=sorted(self.capabilities))
        return result

    def _broker_dispatching(self,message):
        t=self.env.t;started=time.monotonic_ns()
        with self.env.engine.begin() as c:
            c.exec_driver_sql('SET TRANSACTION READ ONLY')
            row=c.execute(sa.select(t.attempts,t.jobs.c.status,t.jobs.c.connector_id,
                (t.attempts.c.lease_until>sa.func.clock_timestamp()).label('active')).join(t.jobs,
                t.jobs.c.operation_id==t.attempts.c.operation_id).join(t.deliveries,
                t.deliveries.c.operation_id==t.jobs.c.operation_id).where(t.jobs.c.operation_id==UUID(message['operation_id']),
                t.deliveries.c.outbox_id==UUID(message['outbox_id']),t.deliveries.c.delivery_id==UUID(message['delivery_id']))
                .order_by(t.attempts.c.ordinal).limit(1)).mappings().one()
            outcome=c.scalar(sa.select(t.observations.c.observation_id).where(t.observations.c.attempt_id==row['attempt_id'],
                t.observations.c.source=='dispatch'))
        finished=time.monotonic_ns()
        if row['active'] is not True or outcome is not None:raise RuntimeError('Original queue attempt is no longer pending inside its deadline')
        key=payment_key(row['operation_id'],row['ordinal'],self.execution_mode) if row['connector_id']=='mock-payments' else str(row['operation_id'])
        sample=lease_rows([(row['operation_id'],row['fenced_version'],row['claim_token'],row['attempt_id'],
            row['lease_until'].timestamp(),row['active'],row['connector_id'],key)],started,finished)[0]
        self.database.append(dict(sample,**message,event='db_broker_inflight_attempt',proof_format='queue-attempt-v1',
            native_job_state=row['status'],attempt_outcome_pending=True))

    def redeliver_during_dispatch(self,held,cancelled,emit):
        if getattr(self.slot,'_lock_fd',None) is None:raise RuntimeError('Queue redelivery requires the owned slot lock')
        DispatchRedelivery(self.slot.redis(),self.slot.redis_db,self.stack.queue,self._broker_admitted,
            self._broker_dispatching,lambda:self.broker_observations(self._broker_since_ns),
            lambda tag:restore_unacked(self.slot.redis_url,tag),emit,protocol='baseline',expect_refusal=False).run(held,cancelled)


class B1Arm(QueueArm):
    name=execution_mode='b1'


class B2Arm(QueueArm):
    name=execution_mode='b2'
