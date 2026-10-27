"""Negative attempt-coverage decision regression tests."""

from dataclasses import replace
from datetime import timedelta
import unittest

from core.attempts import (
    AttemptInventory,
    AttemptObservation,
    AttemptOutcome,
)
from core.contracts import EvidenceKind
from core.recovery import DecisionKind, decide_recovery
from tests.phase_a.test_recovery_policy import (
    OPERATION,
    PAYMENTS,
    T0,
    evidence,
    make_request,
)


def observation(
    attempt_id,
    outcome,
    proof_reference=None,
    **overrides,
):
    fields = dict(
        attempt_id=attempt_id,
        operation_id=OPERATION.operation_id,
        connector_id=OPERATION.connector_id,
        provider_account_id=OPERATION.provider_account_id,
        outcome=outcome,
        proof_reference=proof_reference,
    )
    fields.update(overrides)
    return AttemptObservation(**fields)


def inventory(*attempts, is_complete=True, **overrides):
    fields = dict(
        operation_id=OPERATION.operation_id,
        connector_id=OPERATION.connector_id,
        provider_account_id=OPERATION.provider_account_id,
        contract_version="1.0",
        is_complete=is_complete,
        attempts=tuple(attempts),
    )
    fields.update(overrides)
    return AttemptInventory(**fields)


REJECTED_A = observation(
    "attempt-a",
    AttemptOutcome.PROVEN_NOT_APPLIED,
    "rejection-a",
)

PENDING_B = observation(
    "attempt-b",
    AttemptOutcome.MAY_HAVE_DISPATCHED,
)

REJECTED_B = observation(
    "attempt-b",
    AttemptOutcome.PROVEN_NOT_APPLIED,
    "rejection-b",
)


def negative_decision(attempt_inventory=None, **overrides):
    return decide_recovery(
        make_request(
            attempt_inventory=attempt_inventory,
            evidence=evidence(
                EvidenceKind.PROVEN_NOT_APPLIED,
                reference="operation-negative-claim",
            ),
            **overrides,
        )
    )


class AttemptCoverageTests(unittest.TestCase):

    def test_missing_inventory_blocks_retry(self):
        self.assertEqual(
            negative_decision().kind,
            DecisionKind.VERIFY_LATER,
        )

    def test_rejected_a_pending_b_blocks_retry(self):
        result = negative_decision(
            inventory(REJECTED_A, PENDING_B)
        )
        self.assertEqual(result.kind, DecisionKind.VERIFY_LATER)

    def test_pending_b_after_deadline_escalates(self):
        result = negative_decision(
            inventory(REJECTED_A, PENDING_B),
            now=T0 + timedelta(hours=2),
        )
        self.assertEqual(
            result.kind,
            DecisionKind.ESCALATE_UNRESOLVED,
        )

    def test_both_rejected_allow_authorized_retry(self):
        result = negative_decision(
            inventory(REJECTED_A, REJECTED_B)
        )
        self.assertEqual(
            result.kind,
            DecisionKind.RETRY_AFTER_PROOF,
        )

    def test_incomplete_inventory_blocks_retry(self):
        result = negative_decision(
            inventory(REJECTED_A, is_complete=False)
        )
        self.assertEqual(result.kind, DecisionKind.VERIFY_LATER)

    def test_empty_inventory_is_not_negative_proof(self):
        result = negative_decision(inventory())
        self.assertEqual(result.kind, DecisionKind.VERIFY_LATER)

    def test_confirmed_applied_attempt_conflicts_with_rejection(self):
        applied = observation(
            "attempt-b",
            AttemptOutcome.CONFIRMED_APPLIED,
            "provider-effect-b",
        )
        result = negative_decision(
            inventory(REJECTED_A, applied)
        )
        self.assertEqual(
            result.kind,
            DecisionKind.REJECT_CONFLICT,
        )

    def test_wrong_provider_account_rejected(self):
        result = negative_decision(
            inventory(
                REJECTED_A,
                provider_account_id="account-other",
            )
        )
        self.assertEqual(
            result.kind,
            DecisionKind.REJECT_CONFLICT,
        )

    def test_attempt_bound_to_other_operation_rejected(self):
        foreign = replace(
            REJECTED_A,
            operation_id="op-other",
        )
        result = negative_decision(inventory(foreign))
        self.assertEqual(
            result.kind,
            DecisionKind.REJECT_CONFLICT,
        )

    def test_contract_version_mismatch_rejected(self):
        result = negative_decision(
            inventory(REJECTED_A, contract_version="2.0")
        )
        self.assertEqual(
            result.kind,
            DecisionKind.REJECT_CONFLICT,
        )

    def test_duplicate_attempt_identity_rejected(self):
        with self.assertRaisesRegex(
            ValueError, "duplicate attempt identity"
        ):
            inventory(REJECTED_A, REJECTED_A)

    def test_missing_terminal_proof_rejected(self):
        with self.assertRaisesRegex(
            ValueError, "terminal observation requires"
        ):
            observation(
                "attempt-c",
                AttemptOutcome.PROVEN_NOT_APPLIED,
            )

    def test_local_no_dispatch_requires_proof(self):
        local = observation(
            "attempt-b",
            AttemptOutcome.NEVER_DISPATCHED,
            "durable-local-no-dispatch-proof",
        )
        result = negative_decision(
            inventory(REJECTED_A, local)
        )
        self.assertEqual(
            result.kind,
            DecisionKind.RETRY_AFTER_PROOF,
        )

    def test_negative_proof_still_requires_retry_budget(self):
        result = negative_decision(
            inventory(REJECTED_A, REJECTED_B),
            remaining_retries=0,
        )
        self.assertEqual(
            result.kind,
            DecisionKind.CONFIRM_NOT_APPLIED,
        )

    def test_pending_attempt_cannot_claim_terminal_proof(self):
        with self.assertRaisesRegex(
            ValueError, "pending observation"
        ):
            observation(
                "attempt-b",
                AttemptOutcome.MAY_HAVE_DISPATCHED,
                "fake-terminal-proof",
            )

    def test_not_found_does_not_become_negative_proof(self):
        result = decide_recovery(
            make_request(
                attempt_inventory=inventory(REJECTED_A),
                evidence=evidence(EvidenceKind.NOT_FOUND),
            )
        )
        self.assertEqual(
            result.kind,
            DecisionKind.VERIFY_LATER,
        )


PAYMENTS_OPERATION = replace(OPERATION, connector_id=PAYMENTS.connector_id)


def native_negative_decision(attempt_inventory, **overrides):
    """Proof for one attempt of a native-key operation (D-F16-0002)."""

    return decide_recovery(make_request(
        attempt_inventory=attempt_inventory,
        contract=PAYMENTS,
        original=PAYMENTS_OPERATION,
        candidate=PAYMENTS_OPERATION,
        evidence=evidence(
            EvidenceKind.PROVEN_NOT_APPLIED,
            reference="lookup-absent:attempt-b",
            connector_id=PAYMENTS.connector_id,
        ),
        **overrides,
    ))


def native_inventory(*attempts):
    return inventory(
        *(replace(a, connector_id=PAYMENTS.connector_id) for a in attempts),
        connector_id=PAYMENTS.connector_id,
    )


class NativeKeyIncompleteCoverageTests(unittest.TestCase):
    """Partial proof falls back to the same-key retry basis, never below it."""

    def test_pending_attempt_inside_key_window_retries_same_key(self):
        result = native_negative_decision(
            native_inventory(PENDING_B, REJECTED_A)
        )
        self.assertEqual(result.kind, DecisionKind.RETRY_SAME_KEY)
        self.assertEqual(result.reason, "provider_key_still_protected")

    def test_pending_attempt_at_conservative_expiry_waits(self):
        result = native_negative_decision(
            native_inventory(PENDING_B, REJECTED_A),
            now=T0 + timedelta(minutes=55),
        )
        self.assertEqual(result.kind, DecisionKind.VERIFY_LATER)

    def test_pending_attempt_after_deadline_still_escalates(self):
        result = native_negative_decision(
            native_inventory(PENDING_B, REJECTED_A),
            # Deadline passed while the key window is still open.
            investigation_deadline=T0 + timedelta(minutes=30),
            now=T0 + timedelta(minutes=40),
        )
        self.assertEqual(result.kind, DecisionKind.ESCALATE_UNRESOLVED)

    def test_no_retry_budget_waits(self):
        result = native_negative_decision(
            native_inventory(PENDING_B, REJECTED_A),
            remaining_retries=0,
        )
        self.assertEqual(result.kind, DecisionKind.VERIFY_LATER)

    def test_unauthorized_retry_waits(self):
        result = native_negative_decision(
            native_inventory(PENDING_B, REJECTED_A),
            retry_authorized=False,
        )
        self.assertEqual(result.kind, DecisionKind.VERIFY_LATER)

    def test_contract_without_native_key_still_waits(self):
        self.assertEqual(
            negative_decision(inventory(REJECTED_A, PENDING_B)).kind,
            DecisionKind.VERIFY_LATER,
        )


if __name__ == "__main__":
    unittest.main()
