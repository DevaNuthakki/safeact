"""Phase A: decision-table and boundary tests."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from core.attempts import (
    AttemptInventory,
    AttemptObservation,
    AttemptOutcome,
)

from core.contracts import (
    ConnectorContract,
    Evidence,
    EvidenceKind,
    IdempotencyKeyScope,
    LookupSemantics,
    NativeIdempotency,
    OperationIdentity,
    canonical_payload_hash,
)
from core.recovery import (
    DecisionKind,
    RecoveryRequest,
    decide_recovery,
)


T0 = datetime(
    2026, 10, 15, 12, 0,
    tzinfo=timezone.utc,
)

OPERATION = OperationIdentity(
    operation_id="op-123",
    tenant_id="tenant-a",
    action_type="refund",
    resource_id="payment-1",
    provider_account_id="account-a",
    connector_id="crm-mock",
    payload_sha256=canonical_payload_hash({
        "amount_minor": 1500,
        "currency": "USD",
    }),
)

PAYMENTS = ConnectorContract(
    connector_id="payments-mock",
    version="1.0",
    native_idempotency=NativeIdempotency(
        key_scope=IdempotencyKeyScope.PROVIDER_ACCOUNT_ENDPOINT,
        retention=timedelta(hours=1),
        safety_margin=timedelta(minutes=5),
        protects_concurrent_same_key=True,
        binds_parameters=True,
        contract_reference="mock-profile:payments-v1",
    ),
    lookup_semantics=LookupSemantics.EVENTUAL,
)

CRM = ConnectorContract(
    connector_id="crm-mock",
    version="1.0",
    native_idempotency=None,
    lookup_semantics=LookupSemantics.EVENTUAL,
)

EMAIL = ConnectorContract(
    connector_id="email-mock",
    version="1.0",
    native_idempotency=None,
    lookup_semantics=LookupSemantics.UNAVAILABLE,
)


def complete_negative_inventory():
    return AttemptInventory(
        operation_id=OPERATION.operation_id,
        connector_id=OPERATION.connector_id,
        provider_account_id=OPERATION.provider_account_id,
        contract_version="1.0",
        is_complete=True,
        attempts=(
            AttemptObservation(
                attempt_id="attempt-a",
                operation_id=OPERATION.operation_id,
                connector_id=OPERATION.connector_id,
                provider_account_id=OPERATION.provider_account_id,
                outcome=AttemptOutcome.PROVEN_NOT_APPLIED,
                proof_reference="rejection-101",
            ),
        ),
    )


def evidence(
    kind=EvidenceKind.NOT_FOUND,
    reference=None,
    connector_id="crm-mock",
    operation_id="op-123",
    provider_account_id="account-a",
    contract_version="1.0",
):
    return Evidence(
        kind,
        connector_id,
        operation_id,
        provider_account_id,
        contract_version,
        reference,
    )


def make_request(**overrides):
    values = dict(
        original=OPERATION,
        candidate=OPERATION,
        original_provider_key="key-op-123",
        candidate_provider_key="key-op-123",
        contract=CRM,
        pinned_contract_version="1.0",
        evidence=evidence(),
        first_dispatch_at=T0,
        investigation_deadline=T0 + timedelta(hours=2),
        now=T0 + timedelta(minutes=1),
        remaining_retries=2,
        retry_authorized=True,
    )

    values.update(overrides)

    # Bind test identities to their selected connector.
    if (
        "contract" in overrides
        and "original" not in overrides
        and "candidate" not in overrides
        and "evidence" not in overrides
    ):
        connector_id = values["contract"].connector_id

        values["original"] = replace(
            OPERATION,
            connector_id=connector_id,
        )
        values["candidate"] = values["original"]
        values["evidence"] = evidence(
            connector_id=connector_id,
        )

    return RecoveryRequest(**values)


class RecoveryDecisionTests(unittest.TestCase):

    def assert_decision(self, expected, **overrides):
        decision = decide_recovery(
            make_request(**overrides)
        )
        self.assertEqual(decision.kind, expected)

    def test_late_commit_empty_lookup_crm_must_not_retry(self):
        self.assert_decision(
            DecisionKind.VERIFY_LATER
        )

    def test_non_idempotent_email_inconclusive_must_not_retry(self):
        email_operation = replace(
            OPERATION,
            connector_id="email-mock",
        )

        self.assert_decision(
            DecisionKind.VERIFY_LATER,
            contract=EMAIL,
            original=email_operation,
            candidate=email_operation,
            evidence=evidence(
                EvidenceKind.INCONCLUSIVE,
                connector_id="email-mock",
            ),
        )

    def test_empty_lookup_after_deadline_escalates(self):
        self.assert_decision(
            DecisionKind.ESCALATE_UNRESOLVED,
            now=T0 + timedelta(hours=2),
        )

    def test_native_key_inside_retention_can_retry(self):
        self.assert_decision(
            DecisionKind.RETRY_SAME_KEY,
            contract=PAYMENTS,
        )

    def test_native_key_at_conservative_expiry_is_not_safe(self):
        self.assert_decision(
            DecisionKind.VERIFY_LATER,
            contract=PAYMENTS,
            now=T0 + timedelta(minutes=55),
        )

    def test_concurrency_not_protected_blocks_native_retry(self):
        unsafe = replace(
            PAYMENTS,
            native_idempotency=replace(
                PAYMENTS.native_idempotency,
                protects_concurrent_same_key=False,
            ),
        )

        self.assert_decision(
            DecisionKind.VERIFY_LATER,
            contract=unsafe,
        )

    def test_different_payload_blocks_dispatch(self):
        changed = replace(
            OPERATION,
            payload_sha256=canonical_payload_hash({
                "amount_minor": 2000,
            }),
        )

        self.assert_decision(
            DecisionKind.REJECT_CONFLICT,
            candidate=changed,
        )

    def test_cross_tenant_or_provider_account_identity_conflicts(self):
        cases = (
            ("tenant_id", "tenant-b"),
            ("provider_account_id", "account-b"),
        )

        for field, value in cases:
            with self.subTest(field=field):
                self.assert_decision(
                    DecisionKind.REJECT_CONFLICT,
                    candidate=replace(
                        OPERATION,
                        **{field: value},
                    ),
                )

    def test_changed_provider_key_blocks_dispatch(self):
        self.assert_decision(
            DecisionKind.REJECT_CONFLICT,
            candidate_provider_key="new-key",
        )

    def test_authoritative_applied_effect_is_not_redispatched(self):
        self.assert_decision(
            DecisionKind.CONFIRM_APPLIED,
            evidence=evidence(
                EvidenceKind.APPLIED,
                reference="effect-101",
            ),
        )

    def test_definitive_non_application_allows_bounded_retry(self):
        self.assert_decision(
            DecisionKind.RETRY_AFTER_PROOF,
            attempt_inventory=complete_negative_inventory(),
            evidence=evidence(
                EvidenceKind.PROVEN_NOT_APPLIED,
                reference="rejection-101",
            ),
        )

    def test_non_application_without_retry_budget_is_recorded(self):
        self.assert_decision(
            DecisionKind.CONFIRM_NOT_APPLIED,
            remaining_retries=0,
            attempt_inventory=complete_negative_inventory(),
            evidence=evidence(
                EvidenceKind.PROVEN_NOT_APPLIED,
                reference="rejection-101",
            ),
        )

    def test_retry_authorization_required_even_with_native_key(self):
        self.assert_decision(
            DecisionKind.VERIFY_LATER,
            contract=PAYMENTS,
            retry_authorized=False,
        )

    def test_retry_budget_required_even_with_native_key(self):
        self.assert_decision(
            DecisionKind.VERIFY_LATER,
            contract=PAYMENTS,
            remaining_retries=0,
        )

    def test_definitive_evidence_requires_reference(self):
        with self.assertRaises(ValueError):
            evidence(EvidenceKind.APPLIED)

        with self.assertRaises(ValueError):
            evidence(EvidenceKind.PROVEN_NOT_APPLIED)

    def test_payload_fingerprint_key_order_invariant(self):
        first = canonical_payload_hash({
            "b": [1, True, None],
            "a": "USD",
        })
        second = canonical_payload_hash({
            "a": "USD",
            "b": [1, True, None],
        })

        self.assertEqual(first, second)

    def test_payload_float_is_rejected(self):
        with self.assertRaises(ValueError):
            canonical_payload_hash({
                "amount": 1.5,
            })

    def test_invalid_naive_timestamp_is_rejected(self):
        with self.assertRaises(ValueError):
            make_request(
                now=datetime(2026, 10, 15, 12, 1)
            )

    def test_negative_retry_budget_is_rejected(self):
        with self.assertRaises(ValueError):
            make_request(
                remaining_retries=-1
            )

    def test_evidence_from_another_operation_is_rejected(self):
        self.assert_decision(
            DecisionKind.REJECT_CONFLICT,
            evidence=evidence(
                EvidenceKind.APPLIED,
                reference="effect-101",
                operation_id="op-other",
            ),
        )

    def test_contract_version_change_is_rejected(self):
        self.assert_decision(
            DecisionKind.REJECT_CONFLICT,
            contract=replace(
                CRM,
                version="2.0",
            ),
        )

    def test_strong_lookup_not_found_still_cannot_rule_out_late_commit(self):
        self.assert_decision(
            DecisionKind.VERIFY_LATER,
            contract=replace(
                CRM,
                lookup_semantics=LookupSemantics.STRONG,
            ),
        )

    def test_invalid_retention_contract_is_rejected(self):
        with self.assertRaises(ValueError):
            NativeIdempotency(
                key_scope=IdempotencyKeyScope.PROVIDER_ACCOUNT,
                retention=timedelta(minutes=5),
                safety_margin=timedelta(minutes=5),
                protects_concurrent_same_key=True,
                binds_parameters=True,
                contract_reference="mock-profile:invalid-retention",
            )


if __name__ == "__main__":
    unittest.main()
