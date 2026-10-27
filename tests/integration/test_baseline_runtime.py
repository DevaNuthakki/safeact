"""Independent B3 execution tests over PostgreSQL and actual HTTP mocks."""

import os
import json
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa

from chaos.baselines.environment import Environment
from chaos.baselines.processes import Config
from chaos.baselines.runtime import Task, provider_key
from chaos.baselines.store import Request, admit
from chaos.provider_trace import ObservedProvider
from chaos.recovery_faults import ShortRetentionPayments
from connectors.base import DispatchKind, DispatchResult, LookupTarget
from core.contracts import EvidenceKind
from connectors.crm_mock import MockCrmProvider
from connectors.email_mock import MockEmailProvider
from connectors.http_mock.servers import MockProviderServers
from connectors.ledger import EffectsLedger


ENABLED=(os.environ.get("SAFEACT_RUN_DB_INTEGRATION")=="1" and
         os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS")=="1")


@unittest.skipUnless(ENABLED,"Disposable PostgreSQL integration tests are opt-in")
class BaselineRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.env=Environment().start()
        self.addCleanup(self.env.stop)
        self.ledger=EffectsLedger()
        self.payments=ShortRetentionPayments(ledger=self.ledger)
        self.crm=MockCrmProvider(ledger=self.ledger)
        self.email=MockEmailProvider(ledger=self.ledger)
        observed=tuple(ObservedProvider(kind,mock,self.ledger.record_observation) for kind,mock in
                       (("payments",self.payments),("crm",self.crm),("email",self.email)))
        self.servers=MockProviderServers(ledger=self.ledger,providers=observed,stall_seconds=1.5).start()
        self.addCleanup(self.servers.stop)
        self.directory=tempfile.TemporaryDirectory(prefix="safeact-baseline-runtime-")
        self.addCleanup(self.directory.cleanup)
        self.path=Path(self.directory.name)/"private-config.json"
        self.env.write_config(self.path,self.servers.urls)
        self.context=Config(self.path).runtime()
        self.runtime=self.context.__enter__()
        self.addCleanup(self.context.__exit__,None,None,None)
        self.t=self.env.t

    def submit(self,connector="crm",*,version="q-v2",account=None):
        resource="target_"+uuid4().hex
        payload={"crm":dict(case_id=resource,sku="replacement"),
                 "payments":dict(charge_id=resource,amount_minor=500),
                 "email":dict(customer_id=resource,template="confirmation")}[connector]
        action={"crm":"create_replacement_order","payments":"refund_payment","email":"send_refund_confirmation"}[connector]
        request=Request.from_payload(tenant_id="north",requester_id="requester",request_key="intent-"+uuid4().hex,
            connector_id="mock-"+connector,contract_version=version,provider_account_id=account or "account-"+uuid4().hex,
            action_type=action,resource_id=resource,payload=payload)
        operation=admit(self.env.engine,self.t,request).operation_id
        if connector=="payments":self.payments.register_key(provider_key(operation),version)
        return operation

    def row(self,operation):
        with self.env.engine.connect() as c:
            return dict(c.execute(sa.select(self.t.jobs).where(self.t.jobs.c.operation_id==operation)).mappings().one())

    def task(self):
        tasks=[]
        self.runtime.relay_once(tasks.append)
        self.assertEqual(len(tasks),1)
        return tasks[0]

    def observations(self,operation):
        with self.env.engine.connect() as c:
            return c.execute(sa.select(self.t.observations).where(self.t.observations.c.operation_id==operation)).mappings().all()

    def until(self,operation,state,timeout=5):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.runtime.reconcile(operation)
            if self.row(operation)["status"]==state:return
            time.sleep(.04)
        self.fail(f"B3 did not reach {state}; observed {self.row(operation)['status']}")

    def wait_expired(self,operation):
        deadline=time.monotonic()+6
        while time.monotonic()<deadline:
            row=self.row(operation)
            with self.env.engine.connect() as c:now=self.runtime.now(c)
            if row["lease_until"] and row["lease_until"]<=now:return
            time.sleep(.02)
        self.fail("The actual database lease did not expire")

    def test_each_connector_executes_only_after_committed_claim_and_fence(self):
        for connector in ("crm","payments","email"):
            with self.subTest(connector=connector):
                operation=self.submit(connector)
                observed=[]
                def before(point,details):
                    if point!="before_provider_call":return
                    row=self.row(operation)
                    with self.env.engine.connect() as c:
                        attempts=c.scalar(sa.select(sa.func.count()).select_from(self.t.attempts).where(self.t.attempts.c.operation_id==operation))
                    self.assertEqual((row["status"],attempts),("DISPATCHING",1))
                    observed.append(point)
                self.runtime.hook=before
                task=self.task()
                self.assertEqual(self.runtime.execute(task),"recorded")
                self.assertEqual(self.row(operation)["status"],"SUCCEEDED")
                self.assertEqual(observed,["before_provider_call"])
                self.assertEqual(self.runtime.execute(task),"not_claimable")
                key=provider_key(operation) if connector=="payments" else str(operation)
                self.assertEqual(len(self.ledger.effects_for(key)),1)

    def test_duplicate_concurrent_deliveries_admit_one_claim_and_one_effect(self):
        operation=self.submit()
        task=self.task()
        barrier=threading.Barrier(6,timeout=10)
        def execute():
            barrier.wait()
            return self.runtime.execute(task)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results=[future.result(timeout=15) for future in [pool.submit(execute) for _ in range(6)]]
        self.assertEqual(results.count("recorded"),1)
        self.assertEqual(results.count("not_claimable"),5)
        self.assertEqual(self.row(operation)["status"],"SUCCEEDED")
        self.assertEqual(len(self.ledger.effects_for(str(operation))),1)

    def test_unknown_or_cross_operation_delivery_cannot_claim(self):
        operation=self.submit()
        task=self.task()
        other=self.submit()
        for forged in (Task(operation,task.outbox_id,uuid4()),Task(other,task.outbox_id,task.delivery_id)):
            self.assertEqual(self.runtime.execute(forged),"not_claimable")
        self.assertEqual(self.row(operation)["status"],"RECORDED")
        self.assertEqual(self.ledger.snapshot()[0],())

    def test_two_callers_cannot_fence_the_same_claim_twice(self):
        operation=self.submit()
        claim=self.runtime.claim(self.task())
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=[f.result(timeout=10) for f in [pool.submit(self.runtime.fence,claim) for _ in range(2)]]
        self.assertEqual(sum(f is not None for f in results),1)
        with self.env.engine.connect() as c:
            self.assertEqual(c.scalar(sa.select(sa.func.count()).select_from(self.t.attempts)),1)
        self.assertEqual(self.ledger.snapshot()[0],())

    def test_read_only_refusals_do_not_fire_commit_faults(self):
        operation=self.submit()
        task=self.task()
        seen=[]
        self.runtime.hook=lambda point,details:seen.append(point)
        self.assertIsNone(self.runtime.claim(Task(operation,task.outbox_id,uuid4())))
        self.assertEqual(seen,[])
        claim=self.runtime.claim(task)
        self.assertIsNotNone(self.runtime.fence(claim))
        self.assertIn("after_fence_commit",seen)
        seen.clear()
        self.assertIsNone(self.runtime.fence(claim))
        self.assertEqual(seen,[])

    def test_abandoned_prefence_claims_are_bounded_without_any_provider_call(self):
        operation=self.submit()
        task=self.task()
        self.runtime.lease=timedelta(milliseconds=300)
        for number in range(1,4):
            self.assertIsNotNone(self.runtime.claim(task))
            self.wait_expired(operation)
            self.runtime.reconcile(operation)
            row=self.row(operation)
            self.assertEqual(row["abandoned_claims"],number)
            self.assertEqual(row["status"],"RECORDED" if number<3 else "ESCALATED_UNRESOLVED")
        self.assertEqual(self.ledger.snapshot()[0],())
        self.assertIsNone(self.runtime.claim(task))

    def test_lost_fence_ack_never_sends_an_unprotected_crm_request(self):
        operation=self.submit()
        self.runtime.lease=timedelta(seconds=1)
        def lose(point,details):
            if point=="after_fence_commit":raise ConnectionError("test lost fence acknowledgement")
        self.runtime.hook=lose
        with self.assertRaises(ConnectionError):self.runtime.execute(self.task())
        self.runtime.hook=lambda *_:None
        self.assertEqual(self.row(operation)["status"],"DISPATCHING")
        self.wait_expired(operation)
        self.runtime.reconcile(operation)
        self.assertEqual(self.row(operation)["status"],"UNKNOWN")
        self.assertEqual(self.ledger.calls_for(str(operation)),())
        self.assertTrue(any(o["kind"]=="not_found" for o in self.observations(operation)))

    def test_lost_outcome_ack_keeps_success_and_redelivery_does_not_repeat(self):
        operation=self.submit()
        task=self.task()
        def lose(point,details):
            if point=="after_outcome_commit":raise ConnectionError("test lost outcome acknowledgement")
        self.runtime.hook=lose
        with self.assertRaises(ConnectionError):self.runtime.execute(task)
        self.runtime.hook=lambda *_:None
        self.assertEqual(self.row(operation)["status"],"SUCCEEDED")
        self.assertEqual(self.runtime.execute(task),"not_claimable")
        self.assertEqual(len(self.ledger.calls_for(str(operation))),1)

    def test_actual_late_crm_commit_after_empty_lookup_never_triggers_a_retry(self):
        operation=self.submit()
        held=self.crm.hold_next(respond_now=True)
        self.addCleanup(held.release)
        self.runtime.execute(self.task())
        self.assertTrue(held.started.is_set())
        self.runtime.reconcile(operation)
        self.assertEqual(self.row(operation)["status"],"UNKNOWN")
        self.assertEqual(self.ledger.effects_for(str(operation)),())
        self.assertEqual(len(self.ledger.calls_for(str(operation))),1)

        held.release()
        self.assertTrue(held.wait_committed(2))
        self.until(operation,"SUCCEEDED")
        self.assertEqual(len(self.ledger.effects_for(str(operation))),1)
        self.assertEqual(len(self.ledger.calls_for(str(operation))),1)

    def test_concurrent_reconcilers_reserve_one_actual_lookup(self):
        operation=self.submit()
        self.crm.fail_after_effect=True
        self.crm.index_lag=True
        p=self.runtime.profiles["mock-crm","q-v2"]
        self.runtime.profiles["mock-crm","q-v2"]=replace(p,limits=replace(p.limits,
            lookup_interval=timedelta(seconds=2),lookup_interval_cap=timedelta(seconds=2)))
        self.runtime.execute(self.task())
        barrier=threading.Barrier(6,timeout=10)
        def reconcile():
            barrier.wait()
            return self.runtime.reconcile(operation)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results=[f.result(timeout=15) for f in [pool.submit(reconcile) for _ in range(6)]]
        self.assertEqual(results.count("recorded"),1)
        searches=[e for e in self.ledger.events() if e["event"]=="provider_start" and e.get("method")=="search"]
        self.assertEqual(len(searches),1)
        self.assertEqual(self.row(operation)["status"],"UNKNOWN")

    def test_ineligible_old_jobs_cannot_starve_due_recovery_after_the_scan_limit(self):
        # Synthetic scheduling-only rows; no fabricated provider proof/effect.
        retired=[self.submit() for _ in range(101)]
        with self.env.engine.begin() as c:
            c.execute(self.t.jobs.update().where(self.t.jobs.c.operation_id.in_(retired)).values(
                status="ESCALATED_UNRESOLVED",next_lookup_at=None))
        operation=self.submit()
        self.crm.fail_after_effect=True
        self.runtime.execute(self.task())
        results=self.runtime.reconcile_once(limit=100)
        self.assertEqual(set(results),{operation})
        self.assertEqual(self.row(operation)["status"],"SUCCEEDED")

    def test_abandoned_lookup_lease_recovers_without_repeating_the_dispatch(self):
        operation=self.submit()
        self.crm.fail_after_effect=True
        self.runtime.execute(self.task())
        reserved=self.runtime.reserve_lookup(operation)
        self.assertIsNotNone(reserved)
        row,token=reserved
        self.assertIsNone(self.runtime.reserve_lookup(operation))
        # Simulate the reserved investigator disappearing before its HTTP call.
        while True:
            with self.env.engine.connect() as c:now=self.runtime.now(c)
            if now>=row["lookup_lease_until"]:break
            time.sleep(.05)
        self.until(operation,"SUCCEEDED")
        self.assertEqual(self.row(operation)["lookup_count"],2)
        self.assertEqual(len(self.ledger.calls_for(str(operation))),1)

    def test_expired_lease_during_real_request_retains_late_worker_response(self):
        operation=self.submit(version="q-v2-inflight")
        self.runtime.lease=timedelta(milliseconds=500)
        held=self.crm.hold_next(respond_now=False)
        self.addCleanup(held.release)
        task=self.task()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.runtime.execute,task)
            try:
                self.assertTrue(held.started.wait(3))
                self.wait_expired(operation)
                self.runtime.reconcile(operation)
                self.assertEqual(self.row(operation)["status"],"UNKNOWN")
                self.assertEqual(self.ledger.effects_for(str(operation)),())
            finally:held.release()
            self.assertEqual(future.result(timeout=8),"stale_observed")
        self.until(operation,"SUCCEEDED")
        self.assertEqual(len(self.ledger.calls_for(str(operation))),1)
        self.assertEqual(len(self.ledger.effects_for(str(operation))),1)
        self.assertTrue(any(o["source"]=="dispatch" and o["kind"]=="applied" for o in self.observations(operation)))

    def test_late_result_cannot_overwrite_a_newer_resolved_version(self):
        operation=self.submit()
        self.runtime.lease=timedelta(milliseconds=500)
        claim=self.runtime.claim(self.task())
        fence=self.runtime.fence(claim)
        row=self.row(operation)
        result=self.runtime.connectors["mock-crm"].dispatch(self.runtime.intent(row),operation_id=operation,timeout=1)
        self.wait_expired(operation)
        self.until(operation,"SUCCEEDED")
        resolved=self.row(operation)
        self.assertEqual(self.runtime.record_dispatch(fence,result),"stale_observed")
        self.assertEqual(self.row(operation)["version"],resolved["version"])
        self.assertEqual(len(self.observations(operation)),2)
        self.assertEqual(self.runtime.record_dispatch(fence,result),"replayed")
        self.assertEqual(len(self.observations(operation)),2)
        with self.assertRaisesRegex(RuntimeError,"observation identity"):
            self.runtime.record_dispatch(fence,DispatchResult(DispatchKind.APPLIED,"provider_confirmed","unrelated-reference"))

    def test_ambiguous_email_escalates_with_one_effect_and_no_resend(self):
        operation=self.submit("email")
        self.email.fail_after_effect=True
        self.runtime.execute(self.task())
        for _ in range(3):self.runtime.reconcile(operation)
        self.assertEqual(self.row(operation)["status"],"ESCALATED_UNRESOLVED")
        self.assertEqual(len(self.ledger.calls_for(str(operation))),1)
        self.assertEqual(len(self.ledger.effects_for(str(operation))),1)

    def test_retryable_authoritative_rejection_retries_under_backoff(self):
        operation=self.submit()
        self.crm.reject_next_retryable="actual-provider-rejection"
        self.runtime.execute(self.task())
        self.assertEqual(self.ledger.effects_for(str(operation)),())
        self.until(operation,"RETRY_ELIGIBLE")
        self.runtime.execute(self.task())
        self.assertEqual(self.row(operation)["status"],"SUCCEEDED")
        self.assertEqual(len(self.ledger.calls_for(str(operation))),2)
        self.assertEqual(len(self.ledger.effects_for(str(operation))),1)

    def test_reviewed_contract_does_not_auto_retry_a_proven_rejection(self):
        operation=self.submit()
        self.runtime.reviewed=frozenset({("mock-crm","q-v2")})
        self.crm.reject_next_retryable="actual-provider-rejection"
        self.runtime.execute(self.task())
        for _ in range(3):self.runtime.reconcile(operation)
        self.assertEqual(self.row(operation)["status"],"UNKNOWN")
        self.assertEqual(len(self.ledger.calls_for(str(operation))),1)
        self.assertEqual(self.ledger.effects_for(str(operation)),())

    def test_permanent_provider_rejection_confirms_failure_with_no_effect(self):
        operation=self.submit()
        self.crm.reject_next="actual-case-closed"
        self.runtime.execute(self.task())
        self.assertEqual(self.row(operation)["status"],"FAILED_CONFIRMED")
        self.assertEqual(self.ledger.effects_for(str(operation)),())
        self.assertEqual(self.observations(operation)[0]["reference"],"actual-case-closed")

    def test_native_key_replay_after_lost_reply_stays_one_effect(self):
        operation=self.submit("payments")
        self.payments.fail_after_effect=True
        self.payments.lookup_unavailable=True
        self.runtime.execute(self.task())
        self.until(operation,"RETRY_ELIGIBLE")
        self.runtime.execute(self.task())
        self.assertEqual(self.row(operation)["status"],"SUCCEEDED")
        self.assertEqual(len(self.ledger.effects_for(provider_key(operation))),1)

    def test_native_key_expiry_between_claim_and_fence_refuses_dispatch(self):
        operation=self.submit("payments",version="q-v2-short")
        self.payments.fail_after_effect=True
        self.payments.lookup_unavailable=True
        self.runtime.execute(self.task())
        self.until(operation,"RETRY_ELIGIBLE")
        self.runtime.lease=timedelta(seconds=10)
        claim=self.runtime.claim(self.task())
        self.assertIsNotNone(claim)
        # Wait on the actual DB cutoff; never edit first-fence or evidence rows.
        cutoff=self.row(operation)["first_fence_at"]+timedelta(seconds=4)
        while True:
            with self.env.engine.connect() as c:now=self.runtime.now(c)
            if now>=cutoff:break
            time.sleep(.05)
        self.assertIsNone(self.runtime.fence(claim))
        self.assertEqual(self.row(operation)["status"],"UNKNOWN")
        self.assertEqual(len(self.ledger.calls_for(provider_key(operation))),1)

    def test_lookup_crossing_actual_key_expiry_cannot_become_nonapplication_proof(self):
        operation=self.submit("payments",version="q-v2-short")
        self.payments.fail_after_effect=True
        self.runtime.execute(self.task())
        first=self.row(operation)["first_fence_at"]
        original=self.payments.lookup
        def delayed(key):
            # A real HTTP request enters while valid, then the provider handles
            # it only after its six-second key cache has actually expired.
            while True:
                with self.env.engine.connect() as c:now=self.runtime.now(c)
                if now>=first+timedelta(seconds=6.1):break
                time.sleep(.02)
            return original(key)
        self.payments.lookup=delayed
        while True:
            with self.env.engine.connect() as c:now=self.runtime.now(c)
            if now>=first+timedelta(seconds=3):break
            time.sleep(.02)
        self.runtime.reconcile(operation)
        evidence=self.observations(operation)
        self.assertFalse(any(o["source"]=="lookup" and o["kind"]=="not_applied" for o in evidence))
        self.assertTrue(any(o["source"]=="lookup" and o["kind"]=="not_found" for o in evidence))
        self.assertEqual(self.row(operation)["status"],"UNKNOWN")
        self.assertEqual(len(self.ledger.effects_for(provider_key(operation))),1)
        self.assertEqual(len(self.ledger.calls_for(provider_key(operation))),1)

    def test_old_lookup_proof_does_not_cover_an_attempt_that_started_later(self):
        operation=self.submit("payments")
        self.payments.reject_next_retryable="actual-rejection"
        self.runtime.execute(self.task())
        row=self.row(operation)
        with self.env.engine.connect() as c:now=self.runtime.now(c)
        result=self.runtime.connectors["mock-payments"].lookup(LookupTarget(operation,row["provider_account_id"],
            provider_key(operation),row["payload_sha256"],row["first_fence_at"],now),self.runtime.profile(row))
        self.assertEqual(result.kind,EvidenceKind.PROVEN_NOT_APPLIED)
        with self.env.engine.begin() as c:
            self.runtime.observation(c,observation_id=uuid4(),row=row,source="lookup",kind="not_applied",
                reference=result.reference,retryable=True,details=dict(through_ordinal=1))
        self.until(operation,"RETRY_ELIGIBLE")
        claim=self.runtime.claim(self.task())
        self.assertIsNotNone(self.runtime.fence(claim))
        with self.env.engine.connect() as c:facts,_=self.runtime.history(c,self.row(operation))
        self.assertEqual([f.fact.value for f in facts],["not_applied","ambiguous"])

    def test_application_role_cannot_rewrite_recorded_attempts_or_evidence(self):
        operation=self.submit()
        self.runtime.execute(self.task())
        for table in (self.t.audit,self.t.attempts,self.t.observations):
            with self.subTest(table=table.name),self.assertRaises(sa.exc.DBAPIError):
                with self.env.engine.begin() as c:c.execute(table.delete())

    def test_broker_publish_occurs_after_committed_delivery_and_without_a_db_lock(self):
        operation=self.submit()
        def publish(task):
            with self.env.engine.begin() as c:
                visible=c.scalar(sa.select(self.t.deliveries.c.delivery_id).where(self.t.deliveries.c.delivery_id==task.delivery_id))
                self.assertEqual(visible,task.delivery_id)
                row=c.execute(sa.select(self.t.outbox).where(self.t.outbox.c.outbox_id==task.outbox_id)
                              .with_for_update(nowait=True)).mappings().one()
                self.assertEqual(row["operation_id"],operation)
        self.assertEqual(self.runtime.relay_once(publish),1)

    def test_rate_limit_gates_claim_without_consuming_an_attempt(self):
        key=("mock-crm","q-v2")
        p=self.runtime.profiles[key]
        self.runtime.profiles[key]=replace(p,limits=replace(p.limits,dispatch_burst=2,dispatch_rate_per_second=.01))
        operations=[self.submit(account="shared-rate-account") for _ in range(3)]
        tasks=[]
        self.runtime.relay_once(tasks.append)
        claims=[self.runtime.claim(task) for task in tasks]
        self.assertEqual(sum(c is not None for c in claims),2)
        waiting=next(task.operation_id for task,claim in zip(tasks,claims) if claim is None)
        self.assertEqual(self.row(waiting)["status"],"RECORDED")
        with self.env.engine.connect() as c:self.assertEqual(c.scalar(sa.select(sa.func.count()).select_from(self.t.attempts)),0)
        self.assertEqual(self.ledger.snapshot()[0],())

    def test_breaker_has_one_half_open_probe_and_closes_on_actual_success(self):
        key=("mock-crm","q-v2")
        p=self.runtime.profiles[key]
        self.runtime.profiles[key]=replace(p,limits=replace(p.limits,breaker_threshold=2,breaker_cooldown=timedelta(seconds=1)))
        operations=[self.submit(account="shared-breaker-account") for _ in range(4)]
        tasks=[]
        self.runtime.relay_once(tasks.append)
        self.crm.outage=True
        for task in tasks[:2]:self.runtime.execute(task)
        self.assertIsNone(self.runtime.claim(tasks[2]))
        self.assertEqual(self.row(tasks[2].operation_id)["status"],"RECORDED")
        with self.env.engine.connect() as c:
            cutoff=c.scalar(sa.select(self.t.accounts.c.open_until).where(self.t.accounts.c.provider_account_id=="shared-breaker-account"))
        while True:
            with self.env.engine.connect() as c:now=self.runtime.now(c)
            if now>=cutoff:break
            time.sleep(.02)
        probe=self.runtime.claim(tasks[2])
        self.assertIsNotNone(probe)
        self.assertIsNone(self.runtime.claim(tasks[3]))
        self.crm.outage=False
        fence=self.runtime.fence(probe)
        row=self.row(probe.operation_id)
        result=self.runtime.connectors["mock-crm"].dispatch(self.runtime.intent(row),operation_id=probe.operation_id,timeout=1)
        self.runtime.record_dispatch(fence,result)
        self.assertEqual(self.row(probe.operation_id)["status"],"SUCCEEDED")
        self.assertIsNotNone(self.runtime.claim(tasks[3]))

    def test_retry_after_is_preserved_and_backoff_does_not_trigger_extra_lookups(self):
        operation=self.submit("payments")
        self.payments.rate_limit_next=1
        self.payments.retry_after=.6
        self.runtime.execute(self.task())
        row=self.row(operation)
        self.assertEqual(row["status"],"UNKNOWN")
        with self.env.engine.connect() as c:now=self.runtime.now(c)
        self.assertGreater((row["not_before"]-now).total_seconds(),.4)
        self.runtime.reconcile(operation)
        self.assertEqual(len(self.observations(operation)),1)
        self.until(operation,"RETRY_ELIGIBLE")
        self.runtime.execute(self.task())
        self.assertEqual(self.row(operation)["status"],"SUCCEEDED")
        self.assertEqual(len(self.ledger.effects_for(provider_key(operation))),1)

    def spawn(self,component,*,config=None,task=None):
        command=[sys.executable,"-m","chaos.baselines.processes","--config",str(config or self.path),component]
        if task:command += ["--task",str(task.operation_id),str(task.outbox_id),str(task.delivery_id)]
        path=Path(self.directory.name)/f"{component}-{uuid4().hex}.log"
        log=path.open("wb")
        self.addCleanup(log.close)
        process=subprocess.Popen(command,cwd=Path(__file__).resolve().parents[2],stdout=log,stderr=subprocess.STDOUT,
                                 start_new_session=True)
        def stop():
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGTERM)
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGKILL)
                    process.wait(timeout=5)
        self.addCleanup(stop)
        return process,path

    def test_real_worker_process_killed_after_provider_call_recovers_without_resend(self):
        operation=self.submit()
        task=self.task()
        directory=Path(self.directory.name)/"faults"
        directory.mkdir(mode=0o700)
        armed=directory/"after_provider_call"
        armed.write_text("park");armed.chmod(0o600)
        config=Path(self.directory.name)/"worker-config.json"
        self.env.write_config(config,self.servers.urls,fault_dir=directory)
        process,path=self.spawn("execute",config=config,task=task)
        deadline=time.monotonic()+10
        while not (directory/"parked").exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.02)
        self.assertTrue((directory/"parked").exists(),path.read_text())
        marker=json.loads((directory/"parked").read_text())
        self.assertEqual(marker["point"],"after_provider_call")
        self.assertEqual(marker["pid"],process.pid)
        self.assertEqual(self.row(operation)["status"],"DISPATCHING")
        self.assertEqual(self.observations(operation),[])
        self.assertEqual(len(self.ledger.effects_for(str(operation))),1)
        os.killpg(process.pid,signal.SIGKILL)
        self.assertEqual(process.wait(timeout=5),-signal.SIGKILL)
        self.wait_expired(operation)
        self.until(operation,"SUCCEEDED")
        self.assertEqual(len(self.ledger.calls_for(str(operation))),1)
        self.assertEqual(len(self.ledger.effects_for(str(operation))),1)

    def test_real_reconciler_process_resolves_an_ambiguous_http_dispatch(self):
        operation=self.submit()
        self.crm.fail_after_effect=True
        self.runtime.execute(self.task())
        self.assertEqual(self.row(operation)["status"],"UNKNOWN")
        process,path=self.spawn("reconciler")
        deadline=time.monotonic()+10
        while self.row(operation)["status"]!="SUCCEEDED" and process.poll() is None and time.monotonic()<deadline:time.sleep(.05)
        self.assertEqual(self.row(operation)["status"],"SUCCEEDED",path.read_text())
        self.assertEqual(len(self.ledger.calls_for(str(operation))),1)

    @unittest.skipUnless(os.environ.get("SAFEACT_RUN_BROKER_TESTS")=="1","Real broker checks are opt-in")
    def test_real_prefork_worker_and_relay_recover_a_flushed_queue(self):
        import redis
        from storage.database import local_redis_url
        broker=redis.Redis.from_url(local_redis_url(9),decode_responses=True)
        self.addCleanup(broker.close)
        mine=broker.client_id()
        if any(int(c.get("db",0))==9 and c["id"]!=str(mine) for c in broker.client_list()):
            self.fail("Baseline Redis test database 9 is already in use")
        broker.flushdb()
        self.addCleanup(broker.flushdb)
        operation=self.submit()
        queue="baseline-"+uuid4().hex
        config=Path(self.directory.name)/"broker-config.json"
        self.env.write_config(config,self.servers.urls,broker_url=local_redis_url(9),queue=queue)
        relay,relay_log=self.spawn("relay",config=config)
        deadline=time.monotonic()+10
        while broker.llen(queue)==0 and relay.poll() is None and time.monotonic()<deadline:time.sleep(.05)
        self.assertGreater(broker.llen(queue),0,relay_log.read_text())
        self.assertEqual(self.row(operation)["status"],"RECORDED")
        broker.flushdb()  # Only this test's reserved DB; durable intent remains.
        self.assertEqual(broker.llen(queue),0)
        worker,worker_log=self.spawn("worker",config=config)
        deadline=time.monotonic()+25
        while self.row(operation)["status"]!="SUCCEEDED" and all(p.poll() is None for p in (relay,worker)) and time.monotonic()<deadline:time.sleep(.05)
        self.assertEqual(self.row(operation)["status"],"SUCCEEDED",relay_log.read_text()+worker_log.read_text())
        self.assertEqual(len(self.ledger.calls_for(str(operation))),1)
        self.assertEqual(len(self.ledger.effects_for(str(operation))),1)
