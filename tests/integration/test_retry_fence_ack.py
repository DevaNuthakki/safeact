"""F11 on the retry path: a lost acknowledgement at the retry fence.

The second attempt's fence commits but its acknowledgement is lost.
The worker cannot know whether the fence exists, so it never calls the
provider; the operation is recovered from the database by identity.
"""

import os
import unittest

from uuid import uuid4

from connectors.ledger import EffectsLedger
from connectors.payments import PaymentsConnector
from connectors.payments_mock import MockPaymentsProvider
from core.lifecycle import LifecycleState as S
from core.recovery import DecisionKind as D
from storage import durable_lifecycle as L
from storage.experimental_durable_execution import (
    ExecutionStatus as X,
    ExecutionTask,
    ExecutionWorker,
)
from storage.experimental_recovery_worker import Reconciler
from tests.integration.lifecycle_fixture import DisposableLifecycleDatabase
from tests.integration.profiles_fixture import PAYMENTS_FAST
from tests.integration.timing import margin_for, wait_until_past


LEASE = 1


def lost_fence_ack(point, _connection):
    if point == "after_fence_commit":
        raise ConnectionError("synthetic lost acknowledgement")


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class RetryFenceAckTests(unittest.TestCase):

    def setUp(self):
        self.db = DisposableLifecycleDatabase()
        self.addCleanup(self.db.cleanup)
        self.db.start()
        self.schema = self.db.schema
        self.clock = self.db.connect(self.db.recoverer, autocommit=True)
        self.ledger = EffectsLedger()
        self.provider = MockPaymentsProvider(ledger=self.ledger)

    def test_lost_retry_fence_ack_never_dispatches(self):
        operation_id = self.db.admit(
            request_key="retry-ack", contract_version=PAYMENTS_FAST.version
        )
        self.db.drive_to(
            operation_id, S.RETRY_ELIGIBLE,
            worker=self.db.connect(self.db.worker_a),
            recoverer=self.db.connect(self.db.recoverer),
        )

        result = ExecutionWorker(
            connect=lambda: self.db.connect(self.db.worker_a),
            schema=self.schema,
            connectors=[PaymentsConnector(self.provider)],
            lease_seconds=LEASE, fault_hook=lost_fence_ack,
        ).execute(ExecutionTask(operation_id, uuid4(), uuid4()))

        self.assertEqual((result.status, result.stage),
                         (X.UNCERTAIN, "fence"))
        self.assertFalse(result.provider_called)
        current = L.current_state(self.clock, self.schema, operation_id)
        self.assertIs(current.state, S.DISPATCHING)  # the fence did land
        self.assertEqual(self.db.count(
            "safeact_attempts", operation_id=operation_id
        ), 2)

        # Recovered by identity after the lease: verified, not resent.
        wait_until_past(self.clock, current.lease_expires_at,
                        margin_for(LEASE))
        (decided,) = Reconciler(
            connect=lambda: self.db.connect(self.db.recoverer),
            schema=self.schema,
            connectors=[PaymentsConnector(self.provider)],
        ).reconcile_once()
        # Attempt 1 never got proof, so nothing is concluded yet.
        self.assertEqual(decided.decision, D.VERIFY_LATER)
        self.assertEqual(self.ledger.snapshot(), ((), ()))


if __name__ == "__main__":
    unittest.main()
