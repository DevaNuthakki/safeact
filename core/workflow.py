"""The fixed two-step workflow and its derived status (pure, no I/O).

Scenario (replacement_shipping_v1): a support agent sends a replacement
order. Step 1 charges a shipping fee (payments). Step 2 creates the
replacement order (CRM). If step 2 is confirmed failed after the charge
succeeded, the charge is compensated by a separately identified refund
of exactly that charge. Nothing is compensated on an unknown or
escalated outcome: the order might exist.

Status is derived from the three operations' lifecycle states; it is
not a second state machine. The SQL view in migration 0009 computes the
same function, and a test compares them over every combination.
"""

from enum import Enum

from core.lifecycle import LifecycleState as S


WORKFLOW_TYPE = "replacement_shipping_v1"

CHARGE, ORDER, COMPENSATION = "charge", "order", "compensation"

STEP_ACTIONS = {
    CHARGE: ("mock-payments", "charge_shipping_fee"),
    ORDER: ("mock-crm", "create_replacement_order"),
    COMPENSATION: ("mock-payments", "refund_payment"),
}


class WorkflowStatus(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED_NO_EFFECT = "FAILED_NO_EFFECT"
    COMPENSATING = "COMPENSATING"
    COMPENSATED_VERIFIED = "COMPENSATED_VERIFIED"
    COMPENSATION_UNRESOLVED = "COMPENSATION_UNRESOLVED"
    UNRESOLVED = "UNRESOLVED"


IN_FLIGHT = frozenset({
    S.RECORDED, S.CLAIMED, S.DISPATCHING, S.UNKNOWN, S.VERIFYING,
    S.RETRY_ELIGIBLE,
})


def workflow_status(charge, order=None, compensation=None):
    """Derive the workflow status from its operations' states.

    Raises ValueError for combinations the advancement rules forbid
    (an order before a successful charge, a compensation before a
    confirmed order failure, a cancelled step).
    """

    charge = S(charge)
    order = None if order is None else S(order)
    compensation = None if compensation is None else S(compensation)

    if S.CANCELLED_BEFORE_CLAIM in (charge, order, compensation):
        raise ValueError("workflow steps are never cancelled")
    if order is not None and charge is not S.SUCCEEDED:
        raise ValueError("order admitted before the charge succeeded")
    if compensation is not None and order is not S.FAILED_CONFIRMED:
        raise ValueError("compensation without a confirmed order failure")

    if charge in IN_FLIGHT:
        return WorkflowStatus.IN_PROGRESS
    if charge is S.FAILED_CONFIRMED:
        return WorkflowStatus.FAILED_NO_EFFECT
    if charge is S.ESCALATED_UNRESOLVED:
        return WorkflowStatus.UNRESOLVED

    # The charge succeeded.
    if order is None or order in IN_FLIGHT:
        return WorkflowStatus.IN_PROGRESS
    if order is S.SUCCEEDED:
        return WorkflowStatus.COMPLETED
    if order is S.ESCALATED_UNRESOLVED:
        # The order may exist: never compensated automatically.
        return WorkflowStatus.UNRESOLVED

    # The order is confirmed failed: the charge must be undone.
    if compensation is None or compensation in IN_FLIGHT:
        return WorkflowStatus.COMPENSATING
    if compensation is S.SUCCEEDED:
        return WorkflowStatus.COMPENSATED_VERIFIED
    return WorkflowStatus.COMPENSATION_UNRESOLVED
