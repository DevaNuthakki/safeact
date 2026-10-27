"""Derived workflow status (core.workflow). No database."""

import unittest

from core.lifecycle import LifecycleState as S
from core.workflow import WorkflowStatus as W, workflow_status


class WorkflowStatusTests(unittest.TestCase):

    def test_documented_outcomes(self):
        cases = (
            ((S.RECORDED, None, None), W.IN_PROGRESS),
            ((S.UNKNOWN, None, None), W.IN_PROGRESS),
            ((S.FAILED_CONFIRMED, None, None), W.FAILED_NO_EFFECT),
            ((S.ESCALATED_UNRESOLVED, None, None), W.UNRESOLVED),
            ((S.SUCCEEDED, None, None), W.IN_PROGRESS),
            ((S.SUCCEEDED, S.VERIFYING, None), W.IN_PROGRESS),
            ((S.SUCCEEDED, S.SUCCEEDED, None), W.COMPLETED),
            ((S.SUCCEEDED, S.ESCALATED_UNRESOLVED, None), W.UNRESOLVED),
            ((S.SUCCEEDED, S.FAILED_CONFIRMED, None), W.COMPENSATING),
            ((S.SUCCEEDED, S.FAILED_CONFIRMED, S.UNKNOWN), W.COMPENSATING),
            ((S.SUCCEEDED, S.FAILED_CONFIRMED, S.SUCCEEDED),
             W.COMPENSATED_VERIFIED),
            ((S.SUCCEEDED, S.FAILED_CONFIRMED, S.ESCALATED_UNRESOLVED),
             W.COMPENSATION_UNRESOLVED),
            ((S.SUCCEEDED, S.FAILED_CONFIRMED, S.FAILED_CONFIRMED),
             W.COMPENSATION_UNRESOLVED),
        )
        for states, expected in cases:
            with self.subTest(states=states):
                self.assertIs(workflow_status(*states), expected)

    def test_an_escalated_order_is_never_compensating(self):
        # The order may exist: refunding the charge could be wrong.
        self.assertIs(
            workflow_status(S.SUCCEEDED, S.ESCALATED_UNRESOLVED),
            W.UNRESOLVED,
        )

    def test_forbidden_combinations_raise(self):
        for states in (
            (S.UNKNOWN, S.RECORDED, None),
            (S.SUCCEEDED, S.SUCCEEDED, S.RECORDED),
            (S.SUCCEEDED, None, S.RECORDED),
            (S.SUCCEEDED, S.ESCALATED_UNRESOLVED, S.RECORDED),
        ):
            with self.subTest(states=states):
                with self.assertRaises(ValueError):
                    workflow_status(*states)


if __name__ == "__main__":
    unittest.main()
