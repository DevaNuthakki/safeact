"""Real HTTP/password review, ordinary row-lock decisions and actual effects."""

import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import httpx2
import psycopg
import sqlalchemy as sa

from chaos.baselines.environment import Environment
from chaos.baselines.identity import NO_SECRET_LOGGING
from chaos.baselines.processes import Config
from chaos.baselines.runtime import provider_key
from chaos.baselines.store import Request, admit
from chaos.provider_trace import ObservedProvider
from chaos.recovery_faults import ShortRetentionPayments
from connectors.crm_mock import MockCrmProvider
from connectors.email_mock import MockEmailProvider
from connectors.http_mock.servers import MockProviderServers
from connectors.ledger import EffectsLedger
from storage.database import local_admin_settings


ENABLED = (os.environ.get("SAFEACT_RUN_DB_INTEGRATION")=="1" and
           os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS")=="1")


@unittest.skipUnless(ENABLED,"Disposable PostgreSQL human-review tests are opt-in")
class BaselineReviewTests(unittest.TestCase):
    def setUp(self):
        self.env=Environment().start();self.addCleanup(self.env.stop);self.t=self.env.t
        self.ledger=EffectsLedger();self.payments=ShortRetentionPayments(ledger=self.ledger)
        self.crm=MockCrmProvider(ledger=self.ledger);self.email=MockEmailProvider(ledger=self.ledger)
        providers=tuple(ObservedProvider(k,p,self.ledger.record_observation)
                        for k,p in (("payments",self.payments),("crm",self.crm),("email",self.email)))
        self.servers=MockProviderServers(ledger=self.ledger,providers=providers,stall_seconds=1.5).start()
        self.addCleanup(self.servers.stop)
        self.directory=tempfile.TemporaryDirectory(prefix="safeact-b3-review-");self.addCleanup(self.directory.cleanup)
        self.config=Path(self.directory.name)/"private.json";self.env.write_config(self.config,self.servers.urls)
        self.context=Config(self.config).runtime();self.runtime=self.context.__enter__()
        self.addCleanup(self.context.__exit__,None,None,None)
        self.process=None;self.http=None;self.tokens={}

    def rows(self,table):
        with self.env.engine.connect() as c:return [dict(r) for r in c.execute(sa.select(table)).mappings()]

    def row(self,operation):return next(r for r in self.rows(self.t.jobs) if r["operation_id"]==operation)

    def start_api(self):
        if self.http:return
        self.password="local-human-"+uuid4().hex
        roles={"requester":("requester","reviewer"),"investigator":("investigator","reviewer"),
               "reviewer-one":("reviewer",),"reviewer-two":("reviewer",),"other-tenant":("investigator","reviewer")}
        for actor,grants in roles.items():
            self.env.provision_human(actor_id=actor,tenant_id="south" if actor=="other-tenant" else "north",
                username=actor,password=self.password,roles=grants)
        with socket.socket() as lease:lease.bind(("127.0.0.1",0));port=lease.getsockname()[1]
        self.log_path=Path(self.directory.name)/"api.log";self.log=self.log_path.open("wb");self.addCleanup(self.log.close)
        self.process=subprocess.Popen([sys.executable,"-m","chaos.baselines.processes","--config",str(self.config),
            "api","--port",str(port)],stdout=self.log,stderr=subprocess.STDOUT,start_new_session=True)
        self.addCleanup(self.stop_api)
        self.http=httpx2.Client(base_url=f"http://127.0.0.1:{port}",trust_env=False,timeout=10)
        self.addCleanup(self.http.close)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            self.assertIsNone(self.process.poll(),"Baseline review API exited before readiness")
            try:
                if self.http.get("/healthz").status_code==200:break
            except httpx2.RequestError:pass
            time.sleep(.05)
        else:self.fail("Baseline review API did not become ready")
        for actor in roles:
            response=self.http.post("/login",json=dict(username=actor,password=self.password))
            self.assertEqual(response.status_code,200)
            self.tokens[actor]=response.json()["token"]

    def stop_api(self):
        if self.process and self.process.poll() is None:
            self.assertEqual(os.getpgid(self.process.pid),self.process.pid)
            os.killpg(self.process.pid,signal.SIGTERM)
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid,signal.SIGKILL);self.process.wait(timeout=5);self.fail("Review API did not stop")

    def call(self,method,path,*,actor="investigator",body=None):
        return self.http.request(method,path,json=body,headers={"Authorization":"Bearer "+self.tokens[actor]})

    def operation(self,kind="crm",version="q-v2",*,rejected=True):
        resource="target-"+uuid4().hex
        payload={"crm":dict(case_id=resource,sku="replacement"),"payments":dict(charge_id=resource,amount_minor=500)}[kind]
        request=Request.from_payload(tenant_id="north",requester_id="requester",request_key="review-"+uuid4().hex,
            connector_id="mock-"+kind,contract_version=version,provider_account_id="account-"+uuid4().hex,
            action_type="create_replacement_order" if kind=="crm" else "refund_payment",resource_id=resource,payload=payload)
        self.env.provision_review_policy("mock-"+kind,version)
        operation=admit(self.env.engine,self.t,request).operation_id
        if kind=="payments":self.payments.register_key(provider_key(operation),version)
        if rejected:
            if kind=="crm":self.crm.reject_next_retryable="actual-retryable-rejection"
            else:self.payments.reject_next_retryable="actual-retryable-rejection"
        else:
            if kind=="crm":self.crm.fail_after_effect=True;self.crm.index_lag=True
            else:self.payments.fail_after_effect=True;self.payments.hide_new_effects=True
        tasks=[];self.runtime.relay_once(tasks.append)
        task=next(t for t in tasks if t.operation_id==operation);self.runtime.execute(task)
        self.assertEqual(self.row(operation)["status"],"UNKNOWN")
        return operation

    def proposal(self,operation,action="PROTECTED_RETRY",ttl=120):
        response=self.call("POST",f"/actions/{operation}/proposals",body=dict(action=action,ttl_seconds=ttl))
        self.assertEqual(response.status_code,200)
        return response.json()

    def decide(self,operation,proposal,*,actor="reviewer-one",key=None,approved=True):
        return self.call("POST",f"/actions/{operation}/proposals/{proposal['proposal_id']}/decisions",actor=actor,
            body=dict(binding_sha256=proposal["binding_sha256"],decision_key=key or "decision-"+uuid4().hex,approved=approved))

    def task(self,operation):
        tasks=[];self.runtime.relay_once(tasks.append)
        return next(t for t in tasks if t.operation_id==operation)

    def wait_backoff(self,operation):
        """Ready work: let the jittered retry backoff pass before an approval.

        Until it has, B3 rightly keeps an approved operation VERIFYING
        (retry_backoff); the CI runner caught two tests assuming otherwise
        (PR #7, 2026-10-25).
        """
        not_before=self.row(operation)["not_before"]
        while not_before is not None:
            with self.env.engine.connect() as c:
                if self.runtime.now(c)>=not_before:return
            time.sleep(.05)

    def until(self,operation,state,timeout=12):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.runtime.reconcile_once()
            if self.row(operation)["status"]==state:return
            time.sleep(.03)
        self.fail("Review operation did not reach "+state)

    def test_password_session_boundary_rejects_wrong_credentials_and_unlisted_fields(self):
        self.start_api()
        for username,password in (("requester","wrong-password"),("absent-human",self.password)):
            response=self.http.post("/login",json=dict(username=username,password=password))
            self.assertEqual(response.status_code,401);self.assertNotIn(password,response.text)
        response=self.http.post("/login",json=dict(username="requester",password=self.password,tenant_id="south"))
        self.assertEqual(response.status_code,422);self.assertNotIn(self.password,response.text)
        operation=self.operation()
        self.assertEqual(self.http.get(f"/actions/{operation}/review").status_code,401)
        self.assertEqual(self.http.get(f"/actions/{operation}/review",headers={"Authorization":"Bearer invented-agent-key"}).status_code,401)
        self.assertEqual(self.call("POST",f"/actions/{operation}/proposals",actor="reviewer-one",body=dict(action="REVERIFY")).status_code,403)
        self.assertEqual(self.call("POST",f"/actions/{operation}/proposals",body=dict(action="ATTEST_APPLIED",reference="invented")).status_code,422)
        self.assertEqual(self.rows(self.t.proposals),[])

    def test_requester_and_proposer_cannot_approve_despite_reviewer_role(self):
        self.start_api();operation=self.operation();proposal=self.proposal(operation)
        for actor in ("requester","investigator"):
            self.assertEqual(self.decide(operation,proposal,actor=actor).status_code,403)
        self.assertEqual(self.rows(self.t.decisions),[]);self.assertEqual(self.rows(self.t.authorizations),[])
        self.assertEqual(len(self.ledger.snapshot()[0]),1)

    def test_tenant_and_operation_bindings_are_enforced_at_the_server(self):
        self.start_api();operation=self.operation();proposal=self.proposal(operation)
        self.assertEqual(self.decide(operation,proposal,actor="other-tenant").status_code,404)
        self.assertEqual(self.call("GET",f"/actions/{operation}/review",actor="other-tenant").status_code,404)
        other=self.operation()
        self.assertEqual(self.decide(other,proposal).status_code,404)
        self.assertEqual(self.rows(self.t.decisions),[])

    def test_reviewers_display_the_actual_persisted_proposal(self):
        self.start_api();operation=self.operation();proposal=self.proposal(operation)
        path=f"/actions/{operation}/proposals/{proposal['proposal_id']}"
        for actor in ('reviewer-one','reviewer-two'):
            response=self.call('GET',path,actor=actor);self.assertEqual(response.status_code,200)
            view=response.json();self.assertEqual(view['binding_sha256'],proposal['binding_sha256'])
            self.assertEqual(view['basis'],proposal['basis']);self.assertEqual(view['proposer_id'],'investigator')
        self.assertEqual(self.rows(self.t.decisions),[])
        self.assertEqual(self.rows(self.t.authorizations),[])

    def test_proposal_read_requires_session_and_exact_tenant_operation(self):
        self.start_api();operation=self.operation();proposal=self.proposal(operation)
        path=f"/actions/{operation}/proposals/{proposal['proposal_id']}"
        self.assertEqual(self.http.get(path).status_code,401)
        self.assertEqual(self.call('GET',path,actor='other-tenant').status_code,404)
        other=self.operation()
        self.assertEqual(self.call('GET',f"/actions/{other}/proposals/{proposal['proposal_id']}",actor='reviewer-one').status_code,404)
        self.assertEqual(self.call('GET',f'/actions/{operation}/proposals/{uuid4()}',actor='reviewer-one').status_code,404)

    def test_real_overlapping_reviewers_commit_one_decision_winner_key_replays(self):
        self.start_api();operation=self.operation();proposal=self.proposal(operation)
        barrier=threading.Barrier(2,timeout=10);intervals={}
        def decide(actor):
            barrier.wait();intervals[actor]=[time.monotonic_ns(),None]
            result=self.decide(operation,proposal,actor=actor,key="key-"+actor)
            intervals[actor][1]=time.monotonic_ns();return actor,result
        with self.env.engine.begin() as holder:
            holder.execute(sa.select(self.t.jobs).where(self.t.jobs.c.operation_id==operation).with_for_update()).one()
            holder_pid=holder.scalar(sa.text("SELECT pg_backend_pid()"))
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(decide,a) for a in ("reviewer-one","reviewer-two")]
                try:
                    with psycopg.connect(**local_admin_settings(),autocommit=True) as observer:
                        deadline=time.monotonic()+4;overlap=False
                        while time.monotonic()<deadline:
                            graph=dict(observer.execute("SELECT pid,pg_blocking_pids(pid) FROM pg_stat_activity WHERE usename=%s",(self.env.role,)).fetchall())
                            def reaches(pid,seen=None):
                                seen=set() if seen is None else seen
                                if pid==holder_pid:return True
                                if pid in seen:return False
                                seen.add(pid);return any(reaches(p,seen) for p in graph.get(pid,()))
                            if sum(pid!=holder_pid and reaches(pid) for pid in graph)>=2:
                                overlap=True
                                self.review_overlap=dict(holder_pid=holder_pid,graph=graph,
                                    observed_at_ns=time.monotonic_ns(),source="pg_blocking_pids")
                                break
                            time.sleep(.01)
                        self.assertTrue(overlap,"Both actual HTTP decisions did not reach the operation lock")
                finally:holder.commit()
                results=[f.result(timeout=12) for f in futures]
        self.assertEqual(sorted(r.status_code for _,r in results),[200,409])
        self.review_overlap["request_intervals"] = intervals
        self.assertLess(max(v[0] for v in intervals.values()),min(v[1] for v in intervals.values()))
        winner=next(a for a,r in results if r.status_code==200)
        self.assertEqual(len(self.rows(self.t.decisions)),1)
        self.assertEqual(self.decide(operation,proposal,actor=winner,key="key-"+winner).json()["status"],"replayed")
        self.until(operation,"RETRY_ELIGIBLE");self.runtime.execute(self.task(operation))
        self.assertEqual(self.row(operation)["status"],"SUCCEEDED")
        self.assertEqual(len(self.rows(self.t.authorization_uses)),1)
        self.assertEqual(len(self.ledger.effects_for(str(operation))),1)
        self.assertEqual(len(self.ledger.calls_for(str(operation))),2)
        self.assertEqual(self.decide(operation,proposal,actor=winner,key="key-"+winner).json()["status"],"replayed")
        self.assertEqual(self.decide(operation,proposal,actor=winner,key="changed-key").status_code,409)

    def test_actual_new_matching_provider_evidence_makes_displayed_proposal_stale(self):
        self.start_api();operation=self.operation("payments","q-v2-eventual",rejected=False)
        proposal=self.proposal(operation)
        self.payments.reveal_hidden_effects();self.until(operation,"SUCCEEDED")
        response=self.decide(operation,proposal)
        self.assertEqual((response.status_code,response.json()["status"]),(409,"stale"))
        self.assertEqual(self.rows(self.t.decisions),[])
        self.assertTrue(any(r["kind"]=="decision_refused_stale" for r in self.rows(self.t.audit)))
        self.assertEqual(len(self.ledger.calls_for(provider_key(operation))),1)
        self.assertEqual(len(self.ledger.effects_for(provider_key(operation))),1)

    def test_policy_revision_change_invalidates_displayed_binding(self):
        self.start_api();operation=self.operation();proposal=self.proposal(operation)
        self.env.provision_review_policy("mock-crm","q-v2",required=True)
        self.assertEqual(self.decide(operation,proposal).json()["status"],"stale")
        self.assertEqual(self.rows(self.t.authorizations),[])

    def test_actual_proposal_expiry_and_wrong_binding_refuse_without_a_decision(self):
        self.start_api();operation=self.operation();proposal=self.proposal(operation,ttl=.2)
        time.sleep(.25)
        self.assertEqual(self.decide(operation,proposal).json()["status"],"stale")
        proposal=self.proposal(operation);proposal["binding_sha256"]="0"*64
        self.assertEqual(self.decide(operation,proposal).json()["status"],"stale")
        self.assertEqual(self.rows(self.t.decisions),[])

    def test_rejecting_a_proposal_creates_no_retry_authorization(self):
        self.start_api();operation=self.operation();proposal=self.proposal(operation)
        response=self.decide(operation,proposal,approved=False,key="reject-this")
        self.assertEqual(response.json()["status"],"accepted")
        self.assertEqual(self.decide(operation,proposal,approved=False,key="reject-this").json()["status"],"replayed")
        self.assertEqual(self.rows(self.t.authorizations),[])
        self.runtime.reconcile(operation)
        self.assertEqual(self.row(operation)["status"],"UNKNOWN")
        self.assertEqual(len(self.ledger.snapshot()[0]),1)

    def test_a_reviewers_decision_key_cannot_authorize_two_operations(self):
        self.start_api();first=self.operation();second=self.operation()
        first_proposal=self.proposal(first);second_proposal=self.proposal(second)
        self.assertEqual(self.decide(first,first_proposal,key="shared-click").status_code,200)
        response=self.decide(second,second_proposal,key="shared-click")
        self.assertEqual((response.status_code,response.json()["status"]),(409,"key_conflict"))
        self.assertEqual(len(self.rows(self.t.decisions)),1);self.assertEqual(len(self.rows(self.t.authorizations)),1)

    def test_expired_retry_authorization_returns_ready_work_to_investigation(self):
        self.start_api();operation=self.operation();proposal=self.proposal(operation,ttl=1.2)
        self.assertEqual(self.decide(operation,proposal).status_code,200)
        self.until(operation,"RETRY_ELIGIBLE");task=self.task(operation)
        time.sleep(1.3)
        self.assertIsNone(self.runtime.claim(task));self.assertEqual(self.row(operation)["status"],"UNKNOWN")
        self.assertEqual(self.rows(self.t.authorization_uses),[])
        fresh=self.proposal(operation);self.assertEqual(self.decide(operation,fresh).status_code,200)
        self.until(operation,"RETRY_ELIGIBLE")
        self.runtime.execute(task)
        self.assertEqual(self.row(operation)["status"],"SUCCEEDED")
        self.assertEqual(len(self.rows(self.t.authorization_uses)),1)

    def test_authorization_expiring_between_claim_and_fence_cannot_dispatch(self):
        self.start_api();operation=self.operation();proposal=self.proposal(operation,ttl=1.2)
        self.assertEqual(self.decide(operation,proposal).status_code,200)
        self.until(operation,"RETRY_ELIGIBLE");claim=self.runtime.claim(self.task(operation));self.assertIsNotNone(claim)
        time.sleep(1.3)
        self.assertIsNone(self.runtime.fence(claim));self.assertEqual(self.row(operation)["status"],"UNKNOWN")
        self.assertEqual(self.rows(self.t.authorization_uses),[]);self.assertEqual(len(self.ledger.snapshot()[0]),1)

    def test_real_lookup_finishing_after_approval_invalidates_claim_before_fence(self):
        self.start_api();operation=self.operation("payments","q-v2-eventual",rejected=False)
        self.payments.reveal_hidden_effects()
        self.wait_backoff(operation)
        entered,release=threading.Event(),threading.Event();old=self.payments.lookup
        def held(key):
            entered.set()
            if not release.wait(8):raise RuntimeError("Test lookup release missing")
            return old(key)
        self.payments.lookup=held
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.runtime.reconcile,operation)
            try:
                self.assertTrue(entered.wait(3));proposal=self.proposal(operation)
                self.assertEqual(self.decide(operation,proposal).status_code,200)
                self.until(operation,"RETRY_ELIGIBLE");claim=self.runtime.claim(self.task(operation));self.assertIsNotNone(claim)
            finally:release.set()
            self.assertEqual(future.result(timeout=8),"stale_observed")
        self.assertIsNone(self.runtime.fence(claim))
        self.until(operation,"SUCCEEDED")
        self.assertEqual(self.rows(self.t.authorization_uses),[])
        self.assertEqual(len(self.ledger.effects_for(provider_key(operation))),1)
        self.assertEqual(len(self.ledger.calls_for(provider_key(operation))),1)

    def test_reverify_is_one_lookup_past_actual_horizon_and_never_a_dispatch(self):
        self.start_api();operation=self.operation(rejected=False)
        self.until(operation,"ESCALATED_UNRESOLVED")
        row=self.row(operation);profile=self.runtime.profile(row)
        while True:
            with self.env.engine.connect() as c:now=self.runtime.now(c)
            if now>=row["escalated_at"]+profile.post_escalation_horizon:break
            time.sleep(.05)
        self.runtime.reconcile(operation)
        before=sum(e['event']=='provider_start' and e.get('method')=='search' for e in self.ledger.events())
        proposal=self.proposal(operation,"REVERIFY")
        self.assertEqual(self.decide(operation,proposal).status_code,200)
        self.runtime.reconcile(operation)
        self.runtime.reconcile(operation)
        after=sum(e['event']=='provider_start' and e.get('method')=='search' for e in self.ledger.events())
        self.assertEqual(after,before+1);self.assertEqual(len(self.rows(self.t.authorization_uses)),1)
        self.assertEqual(self.row(operation)["status"],"ESCALATED_UNRESOLVED")
        self.crm.reindex();fresh=self.proposal(operation,"REVERIFY")
        self.assertEqual(self.decide(operation,fresh).status_code,200);self.runtime.reconcile(operation)
        self.assertEqual(self.row(operation)["status"],"SUCCEEDED")
        self.assertEqual(len(self.rows(self.t.authorization_uses)),2)
        self.assertEqual(len(self.ledger.calls_for(str(operation))),1)
        self.assertEqual(len(self.ledger.effects_for(str(operation))),1)

    def test_late_lookup_resolves_approved_ready_work_without_any_broker_delivery(self):
        self.start_api();operation=self.operation("payments","q-v2-eventual",rejected=False)
        self.payments.reveal_hidden_effects()
        self.wait_backoff(operation)
        entered,release=threading.Event(),threading.Event();old=self.payments.lookup
        def held(key):
            entered.set()
            if not release.wait(8):raise RuntimeError("Test lookup release missing")
            return old(key)
        self.payments.lookup=held
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.runtime.reconcile,operation)
            try:
                self.assertTrue(entered.wait(3));proposal=self.proposal(operation)
                self.assertEqual(self.decide(operation,proposal).status_code,200)
                self.assertEqual(self.row(operation)["status"],"RETRY_ELIGIBLE")
            finally:release.set()
            self.assertEqual(future.result(timeout=8),"stale_observed")
        # Do not publish or execute the new retry. Only reconciliation runs.
        self.runtime.reconcile_once()
        self.assertEqual(self.row(operation)["status"],"SUCCEEDED")
        self.assertEqual(self.rows(self.t.authorization_uses),[])
        self.assertEqual(len(self.ledger.effects_for(provider_key(operation))),1)
        self.assertEqual(len(self.ledger.calls_for(provider_key(operation))),1)

    def test_human_approval_preserves_actual_retry_after_and_avoids_unnecessary_lookup(self):
        self.start_api();self.payments.rate_limit_next=1;self.payments.retry_after=.8
        operation=self.operation("payments",rejected=False);self.payments.fail_after_effect=False
        proposal=self.proposal(operation);self.assertEqual(self.decide(operation,proposal).status_code,200)
        with self.env.engine.connect() as c:now=self.runtime.now(c)
        not_before=self.row(operation)["not_before"]
        self.assertLess(now,not_before)
        tasks=[];self.runtime.relay_once(tasks.append);self.assertEqual(tasks,[])
        self.runtime.reconcile_once()
        self.until(operation,"RETRY_ELIGIBLE")
        with self.env.engine.connect() as c:eligible_at=self.runtime.now(c)
        self.assertGreaterEqual(eligible_at,not_before)
        self.runtime.execute(self.task(operation))
        self.assertEqual(self.row(operation)["status"],"SUCCEEDED")
        self.assertEqual(len(self.ledger.calls_for(provider_key(operation))),2)
        self.assertEqual(len(self.ledger.effects_for(provider_key(operation))),1)
        self.assertFalse(any(e['event']=='provider_start' and e.get('method') in ('lookup','search') for e in self.ledger.events()))

    def test_ambiguous_crm_rate_limit_cannot_gain_retry_authority_from_human(self):
        self.start_api();self.crm.rate_limit_next=1;self.crm.retry_after=.8
        operation=self.operation(rejected=False)
        response=self.call("POST",f"/actions/{operation}/proposals",body=dict(action="PROTECTED_RETRY"))
        self.assertEqual((response.status_code,response.json()["status"]),(409,"ineligible"))
        self.runtime.reconcile(operation)
        self.assertEqual(self.rows(self.t.authorizations),[])
        self.assertEqual(len(self.ledger.calls_for(str(operation))),1)
        self.assertEqual(self.ledger.effects_for(str(operation)),())
        self.assertEqual(self.row(operation)["status"],"UNKNOWN")

    def test_logout_and_disabled_human_sessions_cannot_decide(self):
        self.start_api();operation=self.operation();proposal=self.proposal(operation)
        response=self.call("POST","/logout",actor="reviewer-one")
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.decide(operation,proposal).status_code,401)
        self.env.provision_human(actor_id="reviewer-two",tenant_id="north",username="reviewer-two",password=self.password,
                                 roles=("reviewer",),enabled=False)
        self.assertEqual(self.decide(operation,proposal,actor="reviewer-two").status_code,401)
        self.assertEqual(self.rows(self.t.decisions),[])

    def test_passwords_and_bearer_values_are_absent_from_storage_logs_and_validation(self):
        self.start_api();sessions=self.rows(self.t.sessions)
        self.assertEqual(len(sessions),5)
        for actor,token in self.tokens.items():
            self.assertTrue(any(s["token_sha256"]==sha256(token.encode()).hexdigest() for s in sessions))
        self.assertFalse(any(self.password in str(s) or any(token in str(s) for token in self.tokens.values()) for s in sessions))
        response=self.http.post("/login",json=dict(username="requester",password=self.password,unexpected=self.password))
        self.assertEqual(response.status_code,422);self.assertNotIn(self.password,response.text)
        self.log.flush();logged=self.log_path.read_text()
        self.assertNotIn(self.password,logged)
        for token in self.tokens.values():self.assertNotIn(token,logged)

    def test_ordinary_application_cannot_provision_identity_or_change_review_policy(self):
        self.start_api()
        with self.env.engine.connect() as c:
            for setting,value in NO_SECRET_LOGGING.items():self.assertEqual(c.exec_driver_sql("SHOW "+setting).scalar_one(),value)
        for table,values in ((self.t.humans,dict(actor_id="forged",tenant_id="north",username="forged",password_hash="invalid",roles=["reviewer"])),
                             (self.t.policies,dict(connector_id="forged",contract_version="v1",requires_retry_review=False,revision=1))):
            with self.subTest(table=table.name),self.assertRaises(sa.exc.DBAPIError):
                with self.env.engine.begin() as c:c.execute(table.insert().values(**values))
        for table in (self.t.proposals,self.t.decisions,self.t.authorizations,self.t.authorization_uses):
            with self.subTest(table=table.name),self.assertRaises(sa.exc.DBAPIError):
                with self.env.engine.begin() as c:c.execute(table.delete())


if __name__=="__main__":unittest.main()
