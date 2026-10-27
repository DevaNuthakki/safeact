"""B3's own atomic admission/outbox tests, using ordinary PostgreSQL tables."""

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import psycopg
import sqlalchemy as sa
from psycopg import sql
from sqlalchemy.pool import NullPool

from chaos.baselines.store import Conflict, IncompleteAdmission, Request, Tables, admit
from storage.database import local_admin_settings, local_database_url


ENABLED=(os.environ.get("SAFEACT_RUN_DB_INTEGRATION")=="1" and
         os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS")=="1")


@unittest.skipUnless(ENABLED,"Disposable PostgreSQL integration tests are opt-in")
class BaselineAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables=Tables("safeact_baseline_it_"+uuid4().hex)
        cls.admin=psycopg.connect(**local_admin_settings(),autocommit=True)
        cls.addClassCleanup(cls.admin.close)
        cls.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.tables.schema)))
        cls.addClassCleanup(cls.admin.execute,sql.SQL("DROP SCHEMA {}").format(sql.Identifier(cls.tables.schema)))
        cls.engine=sa.create_engine(local_database_url(),poolclass=NullPool,isolation_level="READ COMMITTED")
        cls.addClassCleanup(cls.engine.dispose)
        cls.tables.metadata.create_all(cls.engine)
        cls.addClassCleanup(cls.tables.metadata.drop_all,cls.engine)

    def request(self,**changes):
        values=dict(tenant_id="north",requester_id="requester",request_key="baseline-"+uuid4().hex,
            connector_id="mock-crm",contract_version="q-v2",provider_account_id="baseline-account",
            action_type="create_replacement_order",resource_id="case_baseline",payload=dict(case_id="case_baseline",sku="original"))
        values.update(changes)
        return Request.from_payload(**values)

    def counts(self):
        with self.engine.connect() as connection:
            return tuple(connection.scalar(sa.select(sa.func.count()).select_from(t))
                         for t in (self.tables.jobs,self.tables.audit,self.tables.outbox))

    def assert_bundle(self,operation):
        t=self.tables
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(sa.select(sa.func.count()).select_from(t.jobs).where(t.jobs.c.operation_id==operation)),1)
            for table in (t.audit,t.outbox):
                self.assertEqual(connection.scalar(sa.select(sa.func.count()).select_from(table).where(table.c.operation_id==operation)),1)

    def test_replay_compares_the_complete_immutable_request(self):
        request=self.request()
        first=admit(self.engine,self.tables,request)
        self.assertTrue(first.created)
        for field,value in (("requester_id","other-human"),("connector_id","other-connector"),("contract_version","other-version"),
                            ("provider_account_id","other-account"),("action_type","other-action"),("resource_id","other-case"),
                            ("payload_canonical",'{"case_id":"case_baseline","sku":"changed"}')):
            with self.subTest(field=field),self.assertRaises(Conflict):
                admit(self.engine,self.tables,replace(request,**{field:value}))
        replay=admit(self.engine,self.tables,request)
        self.assertEqual(replay.operation_id,first.operation_id)
        self.assertFalse(replay.created)
        self.assert_bundle(first.operation_id)

    def test_concurrent_identical_requests_return_every_reply_and_one_bundle(self):
        for _ in range(3):
            request=self.request()
            barrier=threading.Barrier(6,timeout=10)
            def submit():
                barrier.wait()
                return admit(self.engine,self.tables,request)
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures=[pool.submit(submit) for _ in range(6)]
                replies=[f.result(timeout=15) for f in futures]
            self.assertEqual(len(replies),6)
            self.assertEqual(len({r.operation_id for r in replies}),1)
            self.assertEqual(sum(r.created for r in replies),1)
            self.assert_bundle(replies[0].operation_id)

    def test_concurrent_changed_payload_has_one_winner_and_one_conflict(self):
        original=self.request()
        other=replace(original,payload_canonical='{"case_id":"case_baseline","sku":"changed"}')
        barrier=threading.Barrier(2,timeout=10)
        def submit(request):
            barrier.wait()
            try:return admit(self.engine,self.tables,request)
            except Conflict:return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(submit,r) for r in (original,other)]
            replies=[f.result(timeout=15) for f in futures]
        self.assertEqual(sum(r is None for r in replies),1)
        winner=next(r for r in replies if r is not None)
        self.assertTrue(winner.created)
        self.assert_bundle(winner.operation_id)

    def test_outbox_failure_rolls_back_intent_and_audit_together(self):
        before=self.counts()
        def reject(connection,cursor,statement,parameters,context,many):
            if statement.lstrip().startswith("INSERT INTO") and ".baseline_outbox " in statement:
                raise RuntimeError("test publication persistence failure")
        sa.event.listen(self.engine,"before_cursor_execute",reject)
        try:
            with self.assertRaisesRegex(RuntimeError,"publication persistence"):
                admit(self.engine,self.tables,self.request())
        finally:sa.event.remove(self.engine,"before_cursor_execute",reject)
        self.assertEqual(self.counts(),before)

    def test_lost_commit_acknowledgement_recovers_the_original_admission(self):
        request=self.request()
        observed=[]
        def lose(receipt):
            observed.append(receipt.operation_id)
            raise ConnectionError("test reply lost after commit")
        with self.assertRaisesRegex(ConnectionError,"reply lost"):
            admit(self.engine,self.tables,request,after_commit=lose)
        recovered=admit(self.engine,self.tables,request)
        self.assertFalse(recovered.created)
        self.assertEqual(observed,[recovered.operation_id])
        self.assert_bundle(recovered.operation_id)

    def test_tenant_is_part_of_identity_and_requester_is_immutable(self):
        request=self.request()
        north=admit(self.engine,self.tables,request)
        south=admit(self.engine,self.tables,replace(request,tenant_id="south",requester_id="south-human"))
        self.assertNotEqual(north.operation_id,south.operation_id)
        with self.assertRaises(Conflict):admit(self.engine,self.tables,replace(request,requester_id="other-human"))

    def test_missing_initial_bundle_is_refused_without_silent_repair(self):
        request=self.request()
        receipt=admit(self.engine,self.tables,request)
        with self.engine.begin() as connection:
            connection.execute(self.tables.outbox.delete().where(self.tables.outbox.c.operation_id==receipt.operation_id))
        before=self.counts()
        with self.assertRaises(IncompleteAdmission):admit(self.engine,self.tables,request)
        self.assertEqual(self.counts(),before)

    def test_unsupported_isolation_is_refused_before_writing(self):
        other=self.engine.execution_options(isolation_level="REPEATABLE READ")
        before=self.counts()
        with self.assertRaisesRegex(ValueError,"READ COMMITTED"):
            admit(other,self.tables,self.request())
        self.assertEqual(self.counts(),before)

    def test_independent_connection_never_observes_a_partial_bundle(self):
        request=self.request()
        waiting,release=threading.Event(),threading.Event()
        def pause(connection,cursor,statement,parameters,context,many):
            if statement.lstrip().startswith("INSERT INTO") and ".baseline_outbox " in statement:
                waiting.set()
                if not release.wait(8):raise RuntimeError("test observer did not release insertion")
        sa.event.listen(self.engine,"before_cursor_execute",pause)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(admit,self.engine,self.tables,request)
                try:
                    self.assertTrue(waiting.wait(5))
                    with self.engine.connect() as observer:
                        job=observer.execute(sa.select(self.tables.jobs.c.operation_id).where(
                            self.tables.jobs.c.request_key==request.request_key)).all()
                        self.assertEqual(job,[])
                finally:release.set()
                receipt=future.result(timeout=10)
        finally:
            release.set()
            sa.event.remove(self.engine,"before_cursor_execute",pause)
        self.assert_bundle(receipt.operation_id)


if __name__=="__main__":unittest.main()
