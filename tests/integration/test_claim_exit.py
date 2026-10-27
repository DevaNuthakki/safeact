"""Safe exit from CLAIMED (0006).

Nothing is dispatched while CLAIMED: the fence creates the attempt and
the DISPATCHING row in one transaction. So an abandoned claim may leave
CLAIMED, but only through the exit its attempt history allows, only by
its owner or after its lease expired, and a stale claimant that resumes
afterwards can never dispatch.
"""

import os
import unittest

from uuid import uuid4

from psycopg import sql

from connectors.ledger import EffectsLedger
from connectors.payments import PaymentsConnector
from connectors.payments_mock import MockPaymentsProvider
from core.lifecycle import LifecycleState as S
from storage import durable_lifecycle as L
from storage.experimental_durable_execution import (
    ExecutionStatus as X,
    ExecutionTask,
    ExecutionWorker,
)
from storage.experimental_recovery_worker import (
    Reconciler,
    ReconcileStatus as R,
    account_operations,
)
from tests.integration.lifecycle_fixture import (
    DisposableLifecycleDatabase,
    commit,
)
from tests.integration.timing import (
    assert_clearly_before,
    margin_for,
    wait_until_past,
)


LEASE = 2


class SimulatedCrash(BaseException):
    """Process death: nothing in the worker may catch it."""


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class ClaimExitTests(unittest.TestCase):

    def setUp(self):
        self.db = DisposableLifecycleDatabase()
        self.addCleanup(self.db.cleanup)
        self.db.start()
        self.schema = self.db.schema
        self.clock = self.db.connect(self.db.recoverer, autocommit=True)
        self.ledger = EffectsLedger()
        self.provider = MockPaymentsProvider(ledger=self.ledger)
        self.count = 0

    def admit(self, **changes):
        self.count += 1
        return self.db.admit(request_key=f"refund-{self.count}", **changes)

    def claim(self, operation_id, role=None):
        connection = self.db.connect(role or self.db.worker_a)
        held = commit(connection, lambda: L.claim(
            connection, self.schema, operation_id, lease_seconds=LEASE,
        ))
        return connection, held

    def state(self, operation_id):
        current = L.current_state(self.clock, self.schema, operation_id)
        return current.state, current.seq

    def lease(self, operation_id):
        return L.current_state(
            self.clock, self.schema, operation_id
        ).lease_expires_at

    def reconcile(self):
        return Reconciler(
            connect=lambda: self.db.connect(self.db.recoverer),
            schema=self.schema,
            connectors=[PaymentsConnector(self.provider)],
        ).reconcile_once()

    def recover(self, operation_id, seq, to_state):
        recoverer = self.db.connect(self.db.recoverer)
        return commit(recoverer, lambda: L.recovery_transition(
            recoverer, self.schema, operation_id, expected_seq=seq,
            expected_state=S.CLAIMED, to_state=to_state,
            reason="test_claim_exit",
        ))

    def test_crashed_claim_exits_after_lease_and_zombie_cannot_dispatch(self):
        operation_id = self.admit()
        zombie, held = self.claim(operation_id)
        lease = self.lease(operation_id)
        margin = margin_for(LEASE)

        # Clearly before the lease expires: left alone.
        assert_clearly_before(self, self.clock, lease, margin)
        (waiting,) = self.reconcile()
        self.assertEqual(waiting.status, R.WAITING)
        self.assertEqual(self.state(operation_id), (S.CLAIMED, 1))

        # Clearly after: no attempt was ever made, so it may be
        # claimed again.
        wait_until_past(self.clock, lease, margin)
        (moved,) = self.reconcile()
        self.assertEqual(moved.state, S.RETRY_ELIGIBLE)
        self.assertEqual(self.state(operation_id), (S.RETRY_ELIGIBLE, 2))

        # The stale claimant resumes: the fence refuses it.
        with self.assertRaises(L.LifecycleRejected):
            L.begin_dispatch(zombie, self.schema, held, lease_seconds=LEASE)
        zombie.rollback()
        self.assertEqual(
            self.db.count("safeact_attempts", operation_id=operation_id), 0
        )
        self.assertEqual(self.ledger.snapshot(), ((), ()))

    def test_worker_crash_after_claim_is_recovered_without_dispatch(self):
        operation_id = self.admit()

        def crash(point, _connection):
            if point == "after_claim_commit":
                raise SimulatedCrash()

        worker = ExecutionWorker(
            connect=lambda: self.db.connect(self.db.worker_a),
            schema=self.schema,
            connectors=[PaymentsConnector(self.provider)],
            lease_seconds=LEASE, fault_hook=crash,
        )
        with self.assertRaises(SimulatedCrash):
            worker.execute(ExecutionTask(operation_id, uuid4(), uuid4()))

        wait_until_past(
            self.clock, self.lease(operation_id), margin_for(LEASE)
        )
        self.reconcile()
        self.assertEqual(self.state(operation_id), (S.RETRY_ELIGIBLE, 2))
        self.assertEqual(self.ledger.snapshot(), ((), ()))

        accounting = account_operations(
            self.db.connect(self.db.recoverer), self.schema
        )
        self.assertEqual(accounting.total, 1)
        self.assertEqual(accounting.retry_eligible, 1)

    def test_owner_may_release_at_once(self):
        operation_id = self.admit()
        connection, held = self.claim(operation_id)

        released = commit(connection, lambda: L.release_claim(
            connection, self.schema, held, reason="worker_gave_up",
        ))
        self.assertEqual(released, (2, S.RETRY_ELIGIBLE))
        events = [e for (e,) in self.db.admin.execute(sql.SQL(
            "SELECT event_type FROM {} WHERE operation_id = %s "
            "ORDER BY audit_id"
        ).format(self.db.table("safeact_audit_events")),
            (operation_id,),
        ).fetchall()]
        self.assertEqual(events[-1], "LIFECYCLE_CLAIM_RELEASED")

    def test_only_the_owner_may_release(self):
        operation_id = self.admit()
        _, held = self.claim(operation_id)

        other = self.db.connect(self.db.worker_b)
        with self.assertRaises(L.NotClaimOwner):
            L.release_claim(other, self.schema, held, reason="steal")
        other.rollback()

        with self.assertRaises(L.NotClaimOwner):
            forged = held.__class__(
                held.operation_id, uuid4(), held.seq, held.intent
            )
            mine = self.db.connect(self.db.worker_a)
            L.release_claim(mine, self.schema, forged, reason="forged")

        stale = held.__class__(held.operation_id, held.claim_id, 0)
        mine = self.db.connect(self.db.worker_a)
        with self.assertRaises(L.StaleTransition):
            L.release_claim(mine, self.schema, stale, reason="stale")

        recoverer = self.db.connect(self.db.recoverer)
        with self.assertRaises(Exception) as caught:
            L.release_claim(recoverer, self.schema, held, reason="nope")
        self.assertEqual(caught.exception.sqlstate, "42501")
        self.assertEqual(self.state(operation_id), (S.CLAIMED, 1))

    def test_recovery_exit_waits_for_the_lease(self):
        operation_id = self.admit()
        self.claim(operation_id)
        lease = self.lease(operation_id)

        assert_clearly_before(self, self.clock, lease, margin_for(LEASE))
        with self.assertRaises(L.LeaseNotExpired):
            self.recover(operation_id, 1, S.RETRY_ELIGIBLE)

        wait_until_past(self.clock, lease, margin_for(LEASE))
        self.assertEqual(self.recover(operation_id, 1, S.RETRY_ELIGIBLE), 2)

    def test_exit_must_match_attempt_history(self):
        operation_id = self.admit()
        self.claim(operation_id)
        wait_until_past(
            self.clock, self.lease(operation_id), margin_for(LEASE)
        )

        # No attempt was ever made: re-verification is not the exit.
        with self.assertRaises(L.IllegalTransition):
            self.recover(operation_id, 1, S.UNKNOWN)
        self.assertEqual(self.state(operation_id), (S.CLAIMED, 1))

    def test_fence_refusal_releases_the_claim(self):
        operation_id = self.admit(contract_version="v9")
        worker = ExecutionWorker(
            connect=lambda: self.db.connect(self.db.worker_a),
            schema=self.schema,
            connectors=[PaymentsConnector(self.provider)],
            lease_seconds=LEASE,
        )
        result = worker.execute(ExecutionTask(operation_id, uuid4(), uuid4()))
        self.assertEqual(result.status, X.NOT_CLAIMABLE)
        self.assertEqual(result.stage, "fence")
        self.assertEqual(self.state(operation_id), (S.RETRY_ELIGIBLE, 2))
        self.assertEqual(self.ledger.snapshot(), ((), ()))


if __name__ == "__main__":
    unittest.main()
