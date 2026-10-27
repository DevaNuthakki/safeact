"""Durable investigation deadlines and evidence-gated reopening (0005).

The SQL rules are exercised through the protected functions directly,
as the recovery role, so they hold even if the reconciler is bypassed.
The reconciler's post-escalation pass is exercised against the CRM
mock. Time-dependent assertions are clearly before or clearly after
each boundary, on the database clock.
"""

import os
import unittest

from uuid import uuid4

from psycopg import sql

from connectors.crm import CrmConnector
from connectors.crm_mock import MockCrmProvider
from connectors.ledger import EffectsLedger
from core.attempts import AttemptOutcome
from core.contracts import EvidenceKind
from core.lifecycle import LifecycleState as S
from core.recovery import DecisionKind as D
from storage import durable_lifecycle as L
from storage.experimental_durable_execution import (
    ExecutionStatus as X,
    ExecutionTask,
    ExecutionWorker,
)
from storage.experimental_recovery_worker import Reconciler
from tests.integration.lifecycle_fixture import (
    DisposableLifecycleDatabase,
    commit,
)
from tests.integration.profiles_fixture import CRM_FAST, CRM_REOPEN
from tests.integration.timing import (
    assert_clearly_before,
    margin_for,
    wait_until_past,
)


CRM = dict(
    connector_id="mock-crm", action_type="create_replacement_order",
    resource_id="synthetic-case",
    payload={"case_id": "synthetic-case", "sku": "sku-1"},
    provider_idempotency_key=None,
)


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class DeadlineAndReopenSqlTests(unittest.TestCase):

    def setUp(self):
        self.db = DisposableLifecycleDatabase()
        self.addCleanup(self.db.cleanup)
        self.db.start()
        self.schema = self.db.schema
        self.worker = self.db.connect(self.db.worker_a)
        self.recoverer = self.db.connect(self.db.recoverer)
        self.clock = self.db.connect(self.db.recoverer, autocommit=True)
        self.count = 0

    def drive(self, target, profile):
        self.count += 1
        operation_id = self.db.admit(
            request_key=f"crm-{self.count}",
            contract_version=profile.version, **CRM,
        )
        driven = self.db.drive_to(
            operation_id, target,
            worker=self.worker, recoverer=self.recoverer,
        )
        return operation_id, driven

    def status(self, operation_id):
        return commit(self.recoverer, lambda: L.investigation_status(
            self.recoverer, self.schema, operation_id,
        ))

    def move(self, operation_id, expected, target):
        status = self.status(operation_id)
        return self.db.recover(
            self.recoverer, operation_id, status.seq, expected, target
        )

    def evidence(self, driven, kind):
        outcome = {
            EvidenceKind.APPLIED: AttemptOutcome.CONFIRMED_APPLIED,
            EvidenceKind.PROVEN_NOT_APPLIED:
                AttemptOutcome.PROVEN_NOT_APPLIED,
            EvidenceKind.NOT_FOUND: AttemptOutcome.MAY_HAVE_DISPATCHED,
        }[kind]
        decisive = kind is not EvidenceKind.NOT_FOUND
        commit(self.recoverer, lambda: L.record_attempt_outcome(
            self.recoverer, self.schema, driven.fence.attempt_id, outcome,
            evidence_kind=kind,
            reference=f"proof-{uuid4().hex}" if decisive else None,
        ))

    def fence_time(self, operation_id):
        return self.db.admin.execute(sql.SQL(
            "SELECT max(created_at) FROM {} WHERE operation_id = %s "
            "AND to_state = 'DISPATCHING'"
        ).format(self.db.table("safeact_operation_transitions")),
            (operation_id,),
        ).fetchone()[0]

    # -- deadlines -------------------------------------------------------

    def test_deadline_is_latest_dispatch_plus_contract_window(self):
        operation_id, _ = self.drive(S.VERIFYING, CRM_FAST)
        self.assertEqual(
            self.status(operation_id).investigation_deadline,
            self.fence_time(operation_id) + CRM_FAST.investigation_window,
        )

    def test_verify_later_only_before_the_deadline(self):
        operation_id, _ = self.drive(S.VERIFYING, CRM_FAST)
        deadline = self.status(operation_id).investigation_deadline
        margin = margin_for(CRM_FAST.investigation_window)

        assert_clearly_before(self, self.clock, deadline, margin)
        self.move(operation_id, S.VERIFYING, S.UNKNOWN)
        self.move(operation_id, S.UNKNOWN, S.VERIFYING)

        wait_until_past(self.clock, deadline, margin)
        with self.assertRaises(L.DeadlinePassed):
            self.move(operation_id, S.VERIFYING, S.UNKNOWN)

        # Escalation is still open; absence never became proof.
        self.move(operation_id, S.VERIFYING, S.ESCALATED_UNRESOLVED)
        with self.assertRaises(L.EvidenceRequired):
            self.move(operation_id, S.ESCALATED_UNRESOLVED, S.VERIFYING)

    # -- reopen ------------------------------------------------------------

    def test_reopen_without_new_evidence_is_refused(self):
        operation_id, _ = self.drive(S.ESCALATED_UNRESOLVED, CRM_REOPEN)
        with self.assertRaises(L.EvidenceRequired):
            self.move(operation_id, S.ESCALATED_UNRESOLVED, S.VERIFYING)

    def test_evidence_recorded_before_escalation_cannot_reopen(self):
        operation_id, driven = self.drive(S.VERIFYING, CRM_REOPEN)
        self.evidence(driven, EvidenceKind.APPLIED)
        self.move(operation_id, S.VERIFYING, S.ESCALATED_UNRESOLVED)

        with self.assertRaises(L.EvidenceRequired):
            self.move(operation_id, S.ESCALATED_UNRESOLVED, S.VERIFYING)

    def test_non_decisive_evidence_cannot_reopen(self):
        operation_id, driven = self.drive(
            S.ESCALATED_UNRESOLVED, CRM_REOPEN
        )
        self.evidence(driven, EvidenceKind.NOT_FOUND)
        with self.assertRaises(L.EvidenceRequired):
            self.move(operation_id, S.ESCALATED_UNRESOLVED, S.VERIFYING)

    def test_one_evidence_row_justifies_one_reopen(self):
        operation_id, driven = self.drive(
            S.ESCALATED_UNRESOLVED, CRM_REOPEN
        )
        self.evidence(driven, EvidenceKind.APPLIED)
        self.move(operation_id, S.ESCALATED_UNRESOLVED, S.VERIFYING)

        # A reopened operation's deadline has passed: no verify-later.
        with self.assertRaises(L.DeadlinePassed):
            self.move(operation_id, S.VERIFYING, S.UNKNOWN)

        self.move(operation_id, S.VERIFYING, S.ESCALATED_UNRESOLVED)
        with self.assertRaises(L.EvidenceRequired):
            self.move(operation_id, S.ESCALATED_UNRESOLVED, S.VERIFYING)

    def test_reopened_operation_ends_terminal_with_evidence(self):
        operation_id, driven = self.drive(
            S.ESCALATED_UNRESOLVED, CRM_REOPEN
        )
        self.evidence(driven, EvidenceKind.APPLIED)
        self.move(operation_id, S.ESCALATED_UNRESOLVED, S.VERIFYING)
        self.move(operation_id, S.VERIFYING, S.SUCCEEDED)
        self.assertIs(self.status(operation_id).state, S.SUCCEEDED)

    def test_reopen_budget_is_enforced(self):
        operation_id, driven = self.drive(
            S.ESCALATED_UNRESOLVED, CRM_REOPEN
        )
        for _ in range(CRM_REOPEN.max_reopens):
            self.evidence(driven, EvidenceKind.APPLIED)
            self.move(operation_id, S.ESCALATED_UNRESOLVED, S.VERIFYING)
            self.move(operation_id, S.VERIFYING, S.ESCALATED_UNRESOLVED)

        self.evidence(driven, EvidenceKind.APPLIED)
        with self.assertRaises(L.ReopenRefused):
            self.move(operation_id, S.ESCALATED_UNRESOLVED, S.VERIFYING)
        self.assertEqual(
            self.status(operation_id).reopens, CRM_REOPEN.max_reopens
        )

    def test_only_the_recovery_role_can_read_investigation_status(self):
        operation_id, _ = self.drive(S.VERIFYING, CRM_FAST)
        for role in (self.db.worker_a, self.db.stranger):
            connection = self.db.connect(role)
            with self.subTest(role=role):
                with self.assertRaises(Exception) as caught:
                    L.investigation_status(
                        connection, self.schema, operation_id
                    )
                self.assertEqual(caught.exception.sqlstate, "42501")


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class PostEscalationPassTests(unittest.TestCase):

    LEASE = 1

    def setUp(self):
        self.db = DisposableLifecycleDatabase()
        self.addCleanup(self.db.cleanup)
        self.db.start()
        self.schema = self.db.schema
        self.clock = self.db.connect(self.db.recoverer, autocommit=True)
        self.ledger = EffectsLedger()
        self.crm = MockCrmProvider(ledger=self.ledger)

    def connectors(self):
        return [CrmConnector(self.crm)]

    def reconcile(self):
        return Reconciler(
            connect=lambda: self.db.connect(self.db.recoverer),
            schema=self.schema, connectors=self.connectors(),
        ).reconcile_once()

    def status(self, operation_id):
        connection = self.db.connect(self.db.recoverer)
        return commit(connection, lambda: L.investigation_status(
            connection, self.schema, operation_id,
        ))

    def post_escalation_lookups(self, operation_id):
        return self.db.admin.execute(sql.SQL(
            "SELECT count(*) FROM {} WHERE operation_id = %s "
            "AND details->>'pass' = 'post_escalation'"
        ).format(self.db.table("safeact_attempt_outcomes")),
            (operation_id,),
        ).fetchone()[0]

    def escalated_after_late_commit_risk(self):
        """A CRM create whose answer never came; escalated at deadline."""

        operation_id = self.db.admit(
            request_key=f"crm-{uuid4().hex[:8]}",
            contract_version=CRM_REOPEN.version, **CRM,
        )
        held = self.crm.hold_next(respond_now=True)
        worker = ExecutionWorker(
            connect=lambda: self.db.connect(self.db.worker_a),
            schema=self.schema, connectors=self.connectors(),
            lease_seconds=self.LEASE,
        )
        result = worker.execute(ExecutionTask(operation_id, uuid4(), uuid4()))
        self.assertEqual(result.status, X.UNKNOWN)

        wait_until_past(
            self.clock, self.status(operation_id).investigation_deadline,
            margin_for(CRM_REOPEN.investigation_window),
        )
        (escalated,) = self.reconcile()
        self.assertEqual(escalated.decision, D.ESCALATE_UNRESOLVED)
        return operation_id, held

    def test_late_commit_after_escalation_reopens_to_success(self):
        operation_id, held = self.escalated_after_late_commit_risk()
        held.release()
        self.assertTrue(held.wait_committed())

        (result,) = self.reconcile()
        self.assertEqual(result.decision, D.CONFIRM_APPLIED)
        status = self.status(operation_id)
        self.assertIs(status.state, S.SUCCEEDED)
        self.assertEqual(status.reopens, 1)
        effects = self.ledger.effects_for(str(operation_id))
        self.assertEqual(len(effects), 1)
        self.assertEqual(len(self.ledger.calls_for(str(operation_id))), 1)

    def test_pass_respects_interval_and_horizon_and_never_reopens_on_absence(self):
        operation_id, held = self.escalated_after_late_commit_risk()
        escalated_at = self.status(operation_id).escalated_at

        # First pass looks once; absence does not reopen.
        self.reconcile()
        self.assertEqual(self.post_escalation_lookups(operation_id), 1)
        self.assertIs(self.status(operation_id).state, S.ESCALATED_UNRESOLVED)

        # Clearly inside the check interval: no second lookup.
        last = self.status(operation_id).last_lookup_at
        interval = CRM_REOPEN.post_escalation_check_interval
        assert_clearly_before(
            self, self.clock, last + interval, margin_for(interval)
        )
        self.reconcile()
        self.assertEqual(self.post_escalation_lookups(operation_id), 1)

        # Clearly past the interval: one more lookup.
        wait_until_past(self.clock, last + interval, margin_for(interval))
        self.reconcile()
        self.assertEqual(self.post_escalation_lookups(operation_id), 2)

        # Clearly past the horizon: no more lookups, even with a late
        # commit now visible. The operation stays unresolved for a human.
        horizon = CRM_REOPEN.post_escalation_horizon
        wait_until_past(
            self.clock, escalated_at + horizon, margin_for(horizon)
        )
        held.release()
        self.assertTrue(held.wait_committed())
        self.reconcile()
        self.assertEqual(self.post_escalation_lookups(operation_id), 2)
        self.assertIs(self.status(operation_id).state, S.ESCALATED_UNRESOLVED)
        self.assertEqual(len(self.ledger.calls_for(str(operation_id))), 1)


if __name__ == "__main__":
    unittest.main()
