"""F01, F03, F04, F05, F07, F10, F12 across the payments, CRM and email
profiles, plus A3 for email (no automatic retry of ambiguous work).

Effects are counted from each mock's harness ledger, never from what
SafeAct reports. Every test ends with the invariant check: at most one
effect per operation, and every admitted operation accounted for.
"""

import os
import unittest

from uuid import uuid4

import psycopg
from psycopg import sql

from connectors.crm import CrmConnector
from connectors.crm_mock import MockCrmProvider
from connectors.email import EmailConnector
from connectors.email_mock import MockEmailProvider
from connectors.ledger import EffectsLedger
from connectors.payments import PaymentsConnector
from connectors.payments_mock import MockPaymentsProvider
from core.lifecycle import LifecycleState as S
from core.recovery import DecisionKind as D
from storage.experimental_durable_execution import (
    ExecutionStatus as X,
    ExecutionTask,
    ExecutionWorker,
)
from storage.experimental_recovery_worker import (
    Reconciler,
    account_operations,
)
from storage.submission import (
    SubmissionConflict,
    SubmissionValidationError,
    submit_intent,
)
from tests.integration.lifecycle_fixture import DisposableLifecycleDatabase
from storage import durable_lifecycle as L
from tests.integration.profiles_fixture import CRM_FAST, PAYMENTS_FAST
from tests.integration.timing import (
    assert_clearly_before,
    margin_for,
    wait_until_past,
)


LEASE = 1

PROFILES = {
    "payments": dict(
        connector_id="mock-payments", action_type="refund_payment",
        resource_id="synthetic-charge",
        payload={"charge_id": "synthetic-charge", "amount_minor": 1200},
    ),
    "crm": dict(
        connector_id="mock-crm", action_type="create_replacement_order",
        resource_id="synthetic-case",
        payload={"case_id": "synthetic-case", "sku": "sku-1"},
        provider_idempotency_key=None,
    ),
    "email": dict(
        connector_id="mock-email", action_type="send_refund_confirmation",
        resource_id="synthetic-customer",
        payload={"to": "customer@example.invalid", "template": "refund"},
        provider_idempotency_key=None,
    ),
}


class SimulatedCrash(BaseException):
    """Process death: nothing in the worker may catch it."""


def at(point, action):
    def hook(where, connection):
        if where == point:
            action(connection)
    return hook


def crash(_connection):
    raise SimulatedCrash()


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class ConnectorProfileFaultTests(unittest.TestCase):

    def setUp(self):
        self.db = DisposableLifecycleDatabase()
        self.addCleanup(self.db.cleanup)
        self.db.start()
        self.schema = self.db.schema
        self.clock = self.db.connect(self.db.recoverer, autocommit=True)

        self.ledger = EffectsLedger()
        self.payments = MockPaymentsProvider(ledger=self.ledger)
        self.crm = MockCrmProvider(ledger=self.ledger)
        self.email = MockEmailProvider(ledger=self.ledger)
        self.admitted = {}
        self.addCleanup(self.assert_invariants)

    # -- helpers -------------------------------------------------------

    def connectors(self):
        return [
            PaymentsConnector(self.payments),
            CrmConnector(self.crm),
            EmailConnector(self.email),
        ]

    def admit(self, profile, **changes):
        n = len(self.admitted) + 1
        request_key = changes.pop("request_key", f"{profile}-{n}")
        fields = {**PROFILES[profile], **changes}
        operation_id = self.db.admit(request_key=request_key, **fields)
        # Ledger correlation: the derived provider key, or the
        # operation id for connectors without one.
        self.admitted[operation_id] = (
            self.db.provider_key(operation_id) or str(operation_id)
        )
        return operation_id

    def execute(self, operation_id, hook=None):
        worker = ExecutionWorker(
            connect=lambda: self.db.connect(self.db.worker_a),
            schema=self.schema, connectors=self.connectors(),
            lease_seconds=LEASE, fault_hook=hook,
        )
        return worker.execute(ExecutionTask(operation_id, uuid4(), uuid4()))

    def reconcile(self, **options):
        options.setdefault("retry_authorized", True)
        return Reconciler(
            connect=lambda: self.db.connect(self.db.recoverer),
            schema=self.schema, connectors=self.connectors(), **options,
        ).reconcile_once()

    def current(self, operation_id):
        row = self.db.admin.execute(sql.SQL(
            "SELECT state, seq, lease_expires_at FROM {} "
            "WHERE operation_id = %s"
        ).format(self.db.table("safeact_operation_current_state")),
            (operation_id,),
        ).fetchone()
        return S(row[0]), row[1], row[2]

    def state(self, operation_id):
        return self.current(operation_id)[0]

    def fence_time(self, operation_id):
        return self.db.admin.execute(sql.SQL(
            "SELECT min(created_at) FROM {} WHERE operation_id = %s "
            "AND to_state = 'DISPATCHING'"
        ).format(self.db.table("safeact_operation_transitions")),
            (operation_id,),
        ).fetchone()[0]

    def unknown_reason(self, operation_id):
        return self.db.admin.execute(sql.SQL(
            "SELECT reason FROM {} WHERE operation_id = %s "
            "AND from_state = 'DISPATCHING' AND to_state = 'UNKNOWN'"
        ).format(self.db.table("safeact_operation_transitions")),
            (operation_id,),
        ).fetchone()[0]

    def lookup_notes(self, operation_id):
        return [note for (note,) in self.db.admin.execute(sql.SQL(
            "SELECT details->>'note' FROM {} WHERE operation_id = %s "
            "AND details->>'source' = 'reconciler_lookup' "
            "ORDER BY created_at"
        ).format(self.db.table("safeact_attempt_outcomes")),
            (operation_id,),
        ).fetchall()]

    def deadline(self, operation_id):
        """The investigation deadline as the database computes it."""

        connection = self.db.connect(self.db.recoverer)
        status = L.investigation_status(connection, self.schema, operation_id)
        connection.commit()
        return status.investigation_deadline

    def wait_deadline(self, operation_id, window):
        wait_until_past(
            self.clock, self.deadline(operation_id), margin_for(window)
        )

    def wait_lease(self, operation_id):
        # Only a DISPATCHING operation holds a lease; UNKNOWN does not.
        lease = self.current(operation_id)[2]
        if lease is not None:
            wait_until_past(self.clock, lease, margin_for(LEASE))

    def calls(self, operation_id):
        return len(self.ledger.calls_for(self.admitted[operation_id]))

    def effects(self, operation_id):
        return len(self.ledger.effects_for(self.admitted[operation_id]))

    def assert_invariants(self):
        accounting = account_operations(
            self.db.connect(self.db.recoverer), self.schema
        )
        self.assertEqual(accounting.total, len(self.admitted))
        for operation_id in self.admitted:
            self.assertLessEqual(self.effects(operation_id), 1)

    # -- F01: effect commits, response lost ------------------------------

    def test_f01_payments_resolves_from_lookup(self):
        operation_id = self.admit("payments")
        self.payments.fail_after_effect = True
        self.assertEqual(self.execute(operation_id).status, X.UNKNOWN)
        self.wait_lease(operation_id)

        (result,) = self.reconcile()
        self.assertEqual(result.decision, D.CONFIRM_APPLIED)
        self.assertIs(self.state(operation_id), S.SUCCEEDED)
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (1, 1)
        )

    def test_f01_crm_resolves_when_record_is_searchable(self):
        operation_id = self.admit("crm")
        self.crm.fail_after_effect = True
        self.assertEqual(self.execute(operation_id).status, X.UNKNOWN)
        self.wait_lease(operation_id)

        (result,) = self.reconcile()
        self.assertEqual(result.decision, D.CONFIRM_APPLIED)
        self.assertIs(self.state(operation_id), S.SUCCEEDED)
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (1, 1)
        )

    def test_f01_crm_unsearchable_record_escalates_never_fails(self):
        operation_id = self.admit("crm", contract_version=CRM_FAST.version)
        self.crm.drop_from_index = True
        self.crm.fail_after_effect = True
        self.execute(operation_id)
        self.wait_lease(operation_id)
        deadline = (
            self.fence_time(operation_id) + CRM_FAST.investigation_window
        )
        margin = margin_for(CRM_FAST.investigation_window)

        assert_clearly_before(self, self.clock, deadline, margin)
        (later,) = self.reconcile()
        self.assertEqual(later.decision, D.VERIFY_LATER)
        self.assertIs(self.state(operation_id), S.UNKNOWN)

        wait_until_past(self.clock, deadline, margin)
        (escalated,) = self.reconcile()
        self.assertEqual(escalated.decision, D.ESCALATE_UNRESOLVED)
        self.assertIs(self.state(operation_id), S.ESCALATED_UNRESOLVED)
        # The effect exists; SafeAct neither invents failure nor repeats.
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (1, 1)
        )

    def test_f01_email_escalates_at_once_and_is_never_resent(self):
        operation_id = self.admit("email")
        self.email.fail_after_effect = True
        self.execute(operation_id)
        self.wait_lease(operation_id)

        # A3: even with retry authorized, ambiguous email escalates.
        (result,) = self.reconcile(retry_authorized=True)
        self.assertEqual(result.decision, D.ESCALATE_UNRESOLVED)
        self.assertIs(self.state(operation_id), S.ESCALATED_UNRESOLVED)
        self.assertEqual(self.lookup_notes(operation_id), ["no_lookup"])

        # Escalated email is never looked up again or resent.
        (again,) = self.reconcile()
        self.assertEqual(again.status.value, "skipped")
        self.assertEqual(self.lookup_notes(operation_id), ["no_lookup"])
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (1, 1)
        )

    # -- F03: 5xx after commit ---------------------------------------------

    def test_f03_error_after_commit_is_unknown_then_verified(self):
        expected = {
            "payments": S.SUCCEEDED,
            "crm": S.SUCCEEDED,
            "email": S.ESCALATED_UNRESOLVED,
        }
        providers = {
            "payments": self.payments, "crm": self.crm, "email": self.email,
        }
        for profile, final in expected.items():
            with self.subTest(profile=profile):
                operation_id = self.admit(profile)
                providers[profile].server_error_after_effect = True

                self.assertEqual(
                    self.execute(operation_id).status, X.UNKNOWN
                )
                self.assertEqual(
                    self.unknown_reason(operation_id),
                    "ambiguous_error_after_send",
                )
                self.wait_lease(operation_id)
                self.reconcile()
                self.assertIs(self.state(operation_id), final)
                self.assertEqual(
                    (self.calls(operation_id), self.effects(operation_id)),
                    (1, 1),
                )

    # -- F04: duplicate delivery and concurrent submission ----------------

    def test_f04_duplicate_delivery_has_one_claimant_per_profile(self):
        for profile in PROFILES:
            with self.subTest(profile=profile):
                operation_id = self.admit(profile)
                first = self.execute(operation_id)
                second = self.execute(operation_id)
                self.assertEqual(first.status, X.SUCCEEDED)
                self.assertEqual(second.status, X.NOT_CLAIMABLE)
                self.assertFalse(second.provider_called)
                self.assertEqual(
                    (self.calls(operation_id), self.effects(operation_id)),
                    (1, 1),
                )

    def test_f04_resubmission_is_one_operation_and_changed_payload_conflicts(self):
        for profile in ("crm", "email"):
            with self.subTest(profile=profile):
                operation_id = self.admit(profile)
                request = self.db.request(
                    request_key=f"{profile}-{len(self.admitted)}",
                    **PROFILES[profile],
                )
                again = submit_intent(self.db.engine, request)
                self.assertFalse(again.created)
                self.assertEqual(again.operation_id, operation_id)

                # F10 at admission: same key, different payload.
                changed = self.db.request(
                    request_key=request.request_key,
                    **{**PROFILES[profile],
                       "payload": {**request.payload, "changed": True}},
                )
                with self.assertRaises(SubmissionConflict):
                    submit_intent(self.db.engine, changed)

    # -- F05: worker dies after dispatch -----------------------------------

    def test_f05_worker_death_after_dispatch_is_investigated_not_redone(self):
        expected = {
            "payments": S.SUCCEEDED,
            "crm": S.SUCCEEDED,
            "email": S.ESCALATED_UNRESOLVED,
        }
        for profile, final in expected.items():
            with self.subTest(profile=profile):
                operation_id = self.admit(profile)
                with self.assertRaises(SimulatedCrash):
                    self.execute(
                        operation_id, at("after_provider_call", crash)
                    )
                self.assertIs(self.state(operation_id), S.DISPATCHING)

                self.wait_lease(operation_id)
                self.reconcile()
                self.assertIs(self.state(operation_id), final)
                self.assertEqual(
                    (self.calls(operation_id), self.effects(operation_id)),
                    (1, 1),
                )

    # -- F07: lookup empty before late commit -------------------------------

    def test_f07_crm_empty_search_before_late_commit(self):
        operation_id = self.admit("crm", contract_version=CRM_FAST.version)
        held = self.crm.hold_next(respond_now=True)
        self.assertEqual(self.execute(operation_id).status, X.UNKNOWN)
        self.assertEqual(self.effects(operation_id), 0)
        self.wait_lease(operation_id)
        deadline = (
            self.fence_time(operation_id) + CRM_FAST.investigation_window
        )
        margin = margin_for(CRM_FAST.investigation_window)

        assert_clearly_before(self, self.clock, deadline, margin)
        (empty,) = self.reconcile()
        # Empty search is not rejection.
        self.assertEqual(empty.decision, D.VERIFY_LATER)
        self.assertIs(self.state(operation_id), S.UNKNOWN)

        held.release()
        self.assertTrue(held.wait_committed())
        assert_clearly_before(self, self.clock, deadline, margin)
        (found,) = self.reconcile()
        self.assertEqual(found.decision, D.CONFIRM_APPLIED)
        self.assertIs(self.state(operation_id), S.SUCCEEDED)
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (1, 1)
        )

    def test_f07_crm_late_commit_after_deadline_stays_escalated(self):
        operation_id = self.admit("crm", contract_version=CRM_FAST.version)
        held = self.crm.hold_next(respond_now=True)
        self.execute(operation_id)
        deadline = (
            self.fence_time(operation_id) + CRM_FAST.investigation_window
        )
        wait_until_past(
            self.clock, deadline, margin_for(CRM_FAST.investigation_window)
        )

        self.reconcile()
        self.assertIs(self.state(operation_id), S.ESCALATED_UNRESOLVED)

        # The late commit lands; nothing was repeated. Reopening on new
        # evidence is a later step (C3); here it stays unresolved.
        held.release()
        self.assertTrue(held.wait_committed())
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (1, 1)
        )
        self.assertIs(self.state(operation_id), S.ESCALATED_UNRESOLVED)

    def test_f07_payments_in_flight_lookup_is_not_proof(self):
        operation_id = self.admit("payments")
        held = self.payments.hold_next(respond_now=True)
        self.assertEqual(self.execute(operation_id).status, X.UNKNOWN)
        self.wait_lease(operation_id)

        # Retry is not authorized here: this checks the lookup alone.
        (pending,) = self.reconcile(retry_authorized=False)
        # STRONG lookup, but the request is still in flight.
        self.assertEqual(pending.decision, D.VERIFY_LATER)
        self.assertEqual(self.lookup_notes(operation_id), ["in_progress"])

        held.release()
        self.assertTrue(held.wait_committed())
        (found,) = self.reconcile()
        self.assertEqual(found.decision, D.CONFIRM_APPLIED)
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (1, 1)
        )

    def test_f07_email_late_delivery_stays_escalated(self):
        operation_id = self.admit("email")
        held = self.email.hold_next(respond_now=True)
        self.execute(operation_id)
        self.wait_lease(operation_id)
        self.reconcile()
        self.assertIs(self.state(operation_id), S.ESCALATED_UNRESOLVED)

        held.release()
        self.assertTrue(held.wait_committed())
        self.assertEqual(
            (self.calls(operation_id), self.effects(operation_id)), (1, 1)
        )

    # -- F10 / key reuse: provider keys belong to one operation -------------

    def test_second_operation_can_never_reuse_a_provider_key(self):
        first = self.admit("payments")
        self.assertEqual(self.execute(first).status, X.SUCCEEDED)
        key = self.db.provider_key(first)
        self.assertTrue(key.startswith("sa1_"))

        # Same key and payload under another request: refused at
        # admission, because keys are derived server-side.
        with self.assertRaises(SubmissionValidationError):
            self.db.request(
                request_key="payments-reuse",
                provider_idempotency_key=key, **PROFILES["payments"],
            )

        # Bypassing admission does not help: the database recomputes
        # the derivation and enforces one key per provider account.
        original = self.db.admin.execute(sql.SQL(
            "SELECT tenant_id, action_type, resource_id, "
            "provider_account_id, connector_id, contract_version, "
            "payload_sha256, payload_canonical FROM {} "
            "WHERE operation_id = %s"
        ).format(self.db.table("safeact_operations")), (first,)).fetchone()
        for request_key in ("payments-reuse", None):
            with self.subTest(request_key=request_key):
                with self.assertRaises(psycopg.errors.IntegrityError):
                    with self.db.admin.transaction():
                        self.db.admin.execute(sql.SQL(
                            "INSERT INTO {} (operation_id, request_key, "
                            "tenant_id, action_type, resource_id, "
                            "provider_account_id, connector_id, "
                            "contract_version, payload_sha256, "
                            "payload_canonical, provider_idempotency_key) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
                            "%s, %s)"
                        ).format(self.db.table("safeact_operations")), (
                            uuid4(), request_key or "payments-other",
                            *original, key,
                        ))

        holders = self.db.admin.execute(sql.SQL(
            "SELECT count(*) FROM {} WHERE provider_idempotency_key = %s"
        ).format(self.db.table("safeact_operations")), (key,)).fetchone()[0]
        self.assertEqual(holders, 1)
        self.assertEqual(self.accounting_states(), {S.SUCCEEDED: 1})
        self.assertEqual(len(self.ledger.effects_for(key)), 1)

    def accounting_states(self):
        accounting = account_operations(
            self.db.connect(self.db.recoverer), self.schema
        )
        return dict(accounting.by_state)

    # -- F12: lookup fails or lies outside contract -------------------------

    def test_f12_crm_unavailable_search_escalates_without_inventing(self):
        operation_id = self.admit("crm", contract_version=CRM_FAST.version)
        self.crm.fail_after_effect = True
        self.crm.search_unavailable = True
        self.execute(operation_id)
        self.wait_lease(operation_id)

        (later,) = self.reconcile()
        self.assertEqual(later.decision, D.VERIFY_LATER)
        self.wait_deadline(operation_id, CRM_FAST.investigation_window)
        (escalated,) = self.reconcile()
        self.assertEqual(escalated.decision, D.ESCALATE_UNRESOLVED)
        self.assertEqual(
            self.lookup_notes(operation_id),
            ["lookup_unavailable", "lookup_unavailable"],
        )

    def test_f12_crm_search_returning_other_data_is_not_success(self):
        operation_id = self.admit("crm", contract_version=CRM_FAST.version)
        self.crm.fail_after_effect = True
        self.crm.corrupt_search = True
        self.execute(operation_id)
        self.wait_deadline(operation_id, CRM_FAST.investigation_window)

        (result,) = self.reconcile()
        self.assertEqual(result.decision, D.ESCALATE_UNRESOLVED)
        self.assertIs(self.state(operation_id), S.ESCALATED_UNRESOLVED)
        self.assertEqual(
            self.lookup_notes(operation_id),
            ["out_of_contract_binding_mismatch"],
        )

    # -- definitive rejection (no retry until C5) ---------------------------

    def test_definitive_rejection_is_failed_with_no_effect(self):
        providers = {"crm": self.crm, "email": self.email}
        for profile, provider in providers.items():
            with self.subTest(profile=profile):
                operation_id = self.admit(profile)
                provider.reject_next = f"rej-{profile}"
                self.assertEqual(
                    self.execute(operation_id).status, X.FAILED_CONFIRMED
                )
                self.assertEqual(
                    (self.calls(operation_id), self.effects(operation_id)),
                    (1, 0),
                )


if __name__ == "__main__":
    unittest.main()
