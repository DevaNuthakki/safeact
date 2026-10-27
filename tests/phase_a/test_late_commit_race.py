"""
SafeAct Phase A: deterministic late-commit race.

This module demonstrates an intentionally unsafe B0
baseline. It is not production connector logic.

The test harness owns ground truth. Provider lookup
does not expose pending or hidden effects.
"""

import unittest


class DelayedProvider:
    """
    Simulated provider with no native idempotency.

    Dispatch accepts work, then loses the response.
    Accepted requests remain pending until the test
    harness explicitly completes them.
    """

    def __init__(self):
        self._pending = []
        self._effect_ledger = []

    def dispatch(self, operation_id):
        self._pending.append(operation_id)

        raise TimeoutError(
            "Provider accepted request; response lost"
        )

    def lookup(self, operation_id):
        """
        Provider-visible lookup, not authoritative proof
        of non-application.
        """
        if operation_id in self._effect_ledger:
            return "APPLIED"

        return "NOT_FOUND"

    def complete_pending(self):
        """
        Controlled test-harness scheduling point.
        """
        self._effect_ledger.extend(self._pending)
        self._pending.clear()

    @property
    def pending_count(self):
        return len(self._pending)

    @property
    def ground_truth_effects(self):
        """
        Independent evaluation oracle.

        Production connector logic must never use this.
        """
        return tuple(self._effect_ledger)


def assert_no_duplicate_effects(provider, operation_id):
    effect_count = provider.ground_truth_effects.count(
        operation_id
    )

    if effect_count > 1:
        raise AssertionError(
            "prohibited duplicate effect: "
            f"{effect_count} effects for {operation_id}"
        )


class LateCommitRaceTests(unittest.TestCase):

    def test_empty_lookup_does_not_prove_non_application(self):
        provider = DelayedProvider()
        operation_id = "refund-order-123"

        with self.assertRaises(TimeoutError):
            provider.dispatch(operation_id)

        self.assertEqual(provider.pending_count, 1)
        self.assertEqual(
            provider.lookup(operation_id),
            "NOT_FOUND",
        )

        # Original accepted request commits later.
        provider.complete_pending()

        self.assertEqual(
            provider.ground_truth_effects,
            (operation_id,),
        )

    def test_naive_retry_causes_duplicate_effects(self):
        provider = DelayedProvider()
        operation_id = "refund-order-456"

        # Request A is accepted, but response is lost.
        with self.assertRaises(TimeoutError):
            provider.dispatch(operation_id)

        # Lookup is empty while A is still pending.
        self.assertEqual(
            provider.lookup(operation_id),
            "NOT_FOUND",
        )

        # Intentionally unsafe baseline sends request B.
        with self.assertRaises(TimeoutError):
            provider.dispatch(operation_id)

        # Both accepted requests now commit.
        provider.complete_pending()

        self.assertEqual(
            provider.ground_truth_effects,
            (operation_id, operation_id),
        )

        # The safety invariant must detect the violation.
        with self.assertRaisesRegex(
            AssertionError,
            "prohibited duplicate effect",
        ):
            assert_no_duplicate_effects(
                provider,
                operation_id,
            )


if __name__ == "__main__":
    unittest.main()
