"""An actual HTTP lookup that starts valid and finishes after key expiry."""

import os
import time
import unittest
from datetime import timedelta
from uuid import uuid4

from psycopg import sql

from chaos.profiles import QUAL_PROFILES
from chaos.recovery_faults import ShortRetentionPayments
from connectors.http_mock.servers import MockProviderServers
from connectors.http_providers import HttpPaymentsProvider
from connectors.ledger import EffectsLedger
from connectors.payments import PaymentsConnector
from storage import durable_lifecycle as L
from storage.experimental_durable_execution import ExecutionWorker, InMemoryTaskQueue, relay_once
from storage.experimental_recovery_worker import Reconciler
from tests.integration.lifecycle_fixture import DisposableLifecycleDatabase


ENABLED=(os.environ.get("SAFEACT_RUN_DB_INTEGRATION")=="1" and
         os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS")=="1")


@unittest.skipUnless(ENABLED,"Disposable PostgreSQL integration tests are opt-in")
class LookupCompletionTests(unittest.TestCase):
    def test_safeact_does_not_record_a_late_empty_lookup_as_nonapplication_proof(self):
        db=DisposableLifecycleDatabase().start()
        self.addCleanup(db.cleanup)
        db.register_profiles(QUAL_PROFILES)
        ledger=EffectsLedger()
        provider=ShortRetentionPayments(ledger=ledger)
        servers=MockProviderServers(ledger=ledger,stall_seconds=1.5)
        servers.payments=provider
        servers.start()
        self.addCleanup(servers.stop)
        connector=PaymentsConnector(HttpPaymentsProvider(servers.url("payments")))
        self.addCleanup(connector._provider._http.close)
        operation=db.admit(request_key="late-lookup-"+uuid4().hex,contract_version="q-v2-short")
        key=db.provider_key(operation)
        provider.register_key(key,"q-v2-short")
        provider.fail_after_effect=True
        queue=InMemoryTaskQueue()
        worker=ExecutionWorker(connect=lambda:db.connect(db.worker_a),schema=db.schema,connectors=(connector,),lease_seconds=30)
        recoverer=Reconciler(connect=lambda:db.connect(db.recoverer),schema=db.schema,connectors=(connector,),retry_authorized=True)
        relay_once(db.connect(db.worker_a),db.schema,queue)
        worker.run_until_idle(queue)
        first=L.recovery_snapshot(db.admin,db.schema,operation).first_dispatch_at
        self.assertIsNotNone(first)
        original=provider.lookup
        def delayed_lookup(key):
            while db.admin.execute("SELECT clock_timestamp()").fetchone()[0]<first+timedelta(seconds=6.1):
                time.sleep(.02)
            return original(key)
        provider.lookup=delayed_lookup
        while db.admin.execute("SELECT clock_timestamp()").fetchone()[0]<first+timedelta(seconds=3):
            time.sleep(.02)
        recoverer.reconcile_once()
        rows=db.admin.execute(sql.SQL("SELECT evidence_kind, details FROM {}.safeact_attempt_outcomes "
            "WHERE operation_id = %s ORDER BY created_at").format(sql.Identifier(db.schema)),(operation,)).fetchall()
        self.assertFalse(any(kind=="proven_not_applied" for kind,details in rows),rows)
        self.assertEqual(len(ledger.effects_for(key)),1)
        self.assertEqual(len(ledger.calls_for(key)),1)
        self.assertNotIn(L.current_state(db.admin,db.schema,operation).state.value,("FAILED_CONFIRMED","RETRY_ELIGIBLE"))


if __name__=="__main__":unittest.main()
