"""The actions a person may submit through the API, and their payloads.

The server picks the connector from the action type, validates the
payload against the action's schema, takes the target resource from
the payload, and canonicalizes it. The client never chooses a
connector, a tenant or a provider key. Whether this person may do this
to this resource is decided by an exact grant inside PostgreSQL.

Workflow-only actions (charge_shipping_fee) are submitted through the
workflow endpoint, never directly.

CONSOLE_ACTIONS adds what only a signed-in person may submit: a refund of a
Stripe test-mode PaymentIntent (the optional Stripe demo profile,
deploy/demo/stripe_demo.py). It is admitted as ``refund_payment`` on the
``stripe-test`` connector, the action the Stripe adapter executes. Agents get
ACTIONS alone, so no MCP tool can reach Stripe. Without a registered
stripe-test contract and an exact grant, admission refuses it like any other.
"""

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from core.contracts import canonical_payload_bytes


IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RefundPayload(_Payload):
    charge_id: str = Field(pattern=IDENTIFIER)
    amount_minor: int = Field(gt=0, le=10_000_000)


class StripeRefundPayload(_Payload):
    payment_intent: str = Field(pattern=r"^pi_[A-Za-z0-9]{8,64}$")
    amount_minor: int = Field(gt=0, le=10_000_000)


class ReplacementOrderPayload(_Payload):
    case_id: str = Field(pattern=IDENTIFIER)
    sku: str = Field(pattern=IDENTIFIER)


class RefundConfirmationPayload(_Payload):
    customer_id: str = Field(pattern=IDENTIFIER)
    template: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class ActionSpec:
    connector_id: str
    payload: type
    resource_field: str
    description: str
    # The action admitted and executed, when it differs from the API name.
    action_type: str | None = None


ACTIONS = {
    "refund_payment": ActionSpec(
        "mock-payments", RefundPayload, "charge_id",
        "Refund a charge (payments: native idempotency, lookup).",
    ),
    "create_replacement_order": ActionSpec(
        "mock-crm", ReplacementOrderPayload, "case_id",
        "Create a replacement order (CRM: no native key, eventual lookup).",
    ),
    "send_refund_confirmation": ActionSpec(
        "mock-email", RefundConfirmationPayload, "customer_id",
        "Send a refund confirmation (email: irreversible, no lookup).",
    ),
}


CONSOLE_ACTIONS = {
    **ACTIONS,
    "refund_stripe_test_payment": ActionSpec(
        "stripe-test", StripeRefundPayload, "payment_intent",
        "Refund a Stripe test-mode payment (native idempotency, eventual "
        "lookup; test mode only, optional demo profile).",
        action_type="refund_payment",
    ),
}


def canonical(payload: BaseModel) -> str:
    return canonical_payload_bytes(payload.model_dump()).decode("utf-8")
