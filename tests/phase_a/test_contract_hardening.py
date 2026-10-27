"""Phase A adversarial contract regression tests."""

from dataclasses import replace
from datetime import timedelta
import unittest

from core.recovery import DecisionKind, decide_recovery
from tests.phase_a.test_recovery_policy import (
    PAYMENTS,
    T0,
    make_request,
)


class ContractHardeningTests(unittest.TestCase):

    def test_pre_dispatch_decision_time_is_invalid(self):
        with self.assertRaisesRegex(
            ValueError,
            "decision time predates first dispatch",
        ):
            make_request(
                now=T0 - timedelta(seconds=1),
            )

    def test_unsupported_key_scope_is_rejected(self):
        with self.assertRaisesRegex(
            ValueError,
            "known enum",
        ):
            replace(
                PAYMENTS.native_idempotency,
                key_scope="unverified",
            )

    def test_missing_parameter_binding_blocks_retry(self):
        weakened = replace(
            PAYMENTS,
            native_idempotency=replace(
                PAYMENTS.native_idempotency,
                binds_parameters=False,
            ),
        )

        decision = decide_recovery(
            make_request(contract=weakened)
        )

        self.assertEqual(
            decision.kind,
            DecisionKind.VERIFY_LATER,
        )

    def test_missing_contract_reference_rejected(self):
        with self.assertRaisesRegex(
            ValueError,
            "contract reference",
        ):
            replace(
                PAYMENTS.native_idempotency,
                contract_reference="",
            )


if __name__ == "__main__":
    unittest.main()
