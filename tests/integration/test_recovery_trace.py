"""Independent committed recovery samples against the real SQL status API."""

import os
import unittest

from chaos.database_trace import sample_recovery
from connectors.http_trace import correlation_hash
from core.lifecycle import LifecycleState as S
from storage import durable_lifecycle as L
from tests.integration.lifecycle_fixture import DisposableLifecycleDatabase
from tests.integration.profiles_fixture import CRM_REOPEN
from tests.integration.timing import margin_for, wait_until_past


ENABLED = (os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
           and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1")


@unittest.skipUnless(ENABLED, "Explicit disposable PostgreSQL opt-in required")
class RecoveryDatabaseTraceTests(unittest.TestCase):
    def test_database_deadline_crossing_and_committed_escalation_are_independently_visible(self):
        db = DisposableLifecycleDatabase()
        self.addCleanup(db.cleanup)
        db.start()
        op = db.admit(connector_id="mock-crm", action_type="create_replacement_order",
            contract_version=CRM_REOPEN.version, payload={"case_id":"case-1","sku":"sku-1"})
        worker, recoverer = db.connect(db.worker_a), db.connect(db.recoverer)
        driven = db.drive_to(op, S.UNKNOWN, worker=worker, recoverer=recoverer)
        before = sample_recovery(db.admin, db.schema, [str(op)])[0]
        self.assertFalse(before["deadline_passed"])
        self.assertEqual(before["correlation_sha256"], correlation_hash(str(op)))
        status = L.investigation_status(db.admin, db.schema, op)
        wait_until_past(db.admin, status.investigation_deadline, margin_for(CRM_REOPEN.investigation_window))
        after = sample_recovery(db.admin, db.schema, [str(op)])[0]
        self.assertTrue(after["deadline_passed"])
        self.assertEqual(before["investigation_deadline"], after["investigation_deadline"])
        self.assertLess(before["sample_finished_ns"], after["sample_started_ns"])
        seq = db.recover(recoverer, op, driven.seq, S.UNKNOWN, S.VERIFYING)
        db.recover(recoverer, op, seq, S.VERIFYING, S.ESCALATED_UNRESOLVED)
        escalated = sample_recovery(db.admin, db.schema, [str(op)])[0]
        self.assertEqual(escalated["state"], "ESCALATED_UNRESOLVED")
        self.assertIsNotNone(escalated["escalated_at"])
        self.assertGreater(escalated["transition_seq"], after["transition_seq"])


if __name__ == "__main__":
    unittest.main()
