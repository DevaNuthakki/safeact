"""Connect the Phase A provider simulator to recovery policy.

This is an in-memory experiment, not a durable dispatcher.
"""

import unittest

from core.contracts import EvidenceKind
from core.recovery import DecisionKind, decide_recovery

from tests.phase_a.test_late_commit_race import (
    DelayedProvider,
    assert_no_duplicate_effects,
)

from tests.phase_a.test_recovery_policy import (
    OPERATION,
    evidence,
    make_request,
)


class GuardedRaceTests(unittest.TestCase):

    def test_guarded_late_commit_avoids_second_dispatch(self):
        provider = DelayedProvider()
        operation_id = OPERATION.operation_id

        # Original request A is accepted remotely.
        with self.assertRaises(TimeoutError):
            provider.dispatch(operation_id)

        self.assertEqual(provider.pending_count, 1)

        # Provider lookup cannot see pending A.
        observation = provider.lookup(operation_id)

        self.assertEqual(observation, "NOT_FOUND")

        # Normalize the provider's inconclusive observation.
        # The simulation does not grant proof of rejection.
        decision = decide_recovery(
            make_request(
                evidence=evidence(EvidenceKind.NOT_FOUND)
            )
        )

        self.assertEqual(
            decision.kind,
            DecisionKind.VERIFY_LATER,
        )

        self.assertNotIn(
            decision.kind,
            {
                DecisionKind.RETRY_SAME_KEY,
                DecisionKind.RETRY_AFTER_PROOF,
            },
        )

        # No second dispatch is authorized.
        self.assertEqual(provider.pending_count, 1)

        # Original delayed request eventually commits.
        provider.complete_pending()

        # Independent oracle measures remote effects.
        assert_no_duplicate_effects(
            provider,
            operation_id,
        )

        self.assertEqual(
            provider.ground_truth_effects,
            (operation_id,),
        )

        # A later lookup observes the committed effect.
        self.assertEqual(
            provider.lookup(operation_id),
            "APPLIED",
        )

        confirmed = decide_recovery(
            make_request(
                evidence=evidence(
                    EvidenceKind.APPLIED,
                    reference="mock-effect-1",
                )
            )
        )

        self.assertEqual(
            confirmed.kind,
            DecisionKind.CONFIRM_APPLIED,
        )

        # Confirmation must not dispatch again.
        self.assertEqual(
            len(provider.ground_truth_effects),
            1,
        )


if __name__ == "__main__":
    unittest.main()
