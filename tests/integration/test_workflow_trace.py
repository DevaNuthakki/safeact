"""The qualification observer sees committed workflow state on its own connection."""

import os
import unittest

import psycopg

from chaos.workflow_trace import operation_ids, sample_workflows
from connectors.http_trace import correlation_hash
from storage import durable_lifecycle as lifecycle
from storage.experimental_workflow import WorkflowRequest, create_workflow
from tests.integration.lifecycle_fixture import DisposableLifecycleDatabase


ENABLED = (os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
           and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1")


@unittest.skipUnless(ENABLED, "Explicit disposable PostgreSQL opt-in required")
class WorkflowTraceTests(unittest.TestCase):
    def test_uncommitted_claim_is_invisible_until_the_writer_commits(self):
        db = DisposableLifecycleDatabase()
        self.addCleanup(db.cleanup)
        db.start()
        receipt = create_workflow(db.engine, WorkflowRequest(
            tenant_id="synthetic-tenant", request_key="observed-workflow",
            actor_id="synthetic-agent", customer_id="customer", case_id="case",
            sku="sku", fee_minor=500, payments_account_id="mock-payments-account",
            crm_account_id="mock-crm-account"))
        observer = psycopg.connect(**db.settings, autocommit=True)
        self.addCleanup(observer.close)
        writer = db.connect(db.worker_a)
        lifecycle.claim(writer, db.schema, receipt.charge_operation_id, lease_seconds=10)

        first = sample_workflows(observer, db.schema, [str(receipt.workflow_id)])[0]
        self.assertEqual(first["steps"][0]["state"], "RECORDED")
        self.assertEqual(first["steps"][0]["transition_seq"], 0)
        writer.commit()
        second = sample_workflows(observer, db.schema, [str(receipt.workflow_id)])[0]
        self.assertEqual(second["steps"][0]["state"], "CLAIMED")
        self.assertEqual(second["steps"][0]["transition_seq"], 1)
        self.assertLess(first["sample_finished_ns"], second["sample_started_ns"])
        self.assertEqual(operation_ids([second]), [str(receipt.charge_operation_id)])
        self.assertEqual(second["steps"][0]["correlation_sha256"],
                         correlation_hash(db.provider_key(receipt.charge_operation_id)))
        self.assertEqual(second["steps"][0]["payload"],
                         {"customer_id": "customer", "amount_minor": 500})
        self.assertEqual(second["source"], "database")
        self.assertIsNone(second["steps"][0]["compensates_operation_id"])


if __name__ == "__main__":
    unittest.main()
