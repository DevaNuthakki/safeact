"""In-process email provider mock (a test double, never a provider).

Irreversible, no idempotency and no lookup. The only definitive answer
is a rejection before acceptance (for example an invalid recipient),
which sends nothing. Every call and delivery is appended to the
harness's EffectsLedger.
"""

from threading import Lock
from uuid import uuid4

from connectors.mock_support import HeldCall, await_held


class EmailTimeout(Exception):
    """No response. The message may or may not have been sent."""


class EmailServerError(Exception):
    """A 5xx-style response after submission. Says nothing about delivery."""


class EmailRateLimited(Exception):
    """429. Nothing documents whether the message was accepted."""

    def __init__(self, retry_after=None):
        super().__init__("rate limited")
        self.retry_after = retry_after


class EmailRejected(Exception):
    """Rejected before acceptance: nothing was sent."""

    def __init__(self, reference):
        super().__init__(f"message rejected: {reference}")
        self.reference = reference


class MockEmailProvider:

    def __init__(self, *, ledger):
        self._ledger = ledger
        self._lock = Lock()
        self._held = []

        self.fail_before_effect = False
        self.fail_after_effect = False
        self.server_error_after_effect = False
        self.reject_next = None
        self.rate_limit_next = 0
        self.retry_after = None
        self.outage = False

    def send(self, message_id, params, *, timeout=None):
        if type(message_id) is not str or not message_id:
            raise ValueError("message id required")

        with self._lock:
            self._ledger.record_call(message_id, params)

            if self.outage:
                raise EmailTimeout("provider down, nothing sent")

            if self.rate_limit_next:
                self.rate_limit_next -= 1
                raise EmailRateLimited(self.retry_after)

            if self.fail_before_effect:
                self.fail_before_effect = False
                raise EmailTimeout("no response, nothing sent")

            if self.reject_next is not None:
                reference, self.reject_next = self.reject_next, None
                raise EmailRejected(reference)

            held = self._held.pop(0) if self._held else None
            if held is None:
                delivery = self._deliver(message_id)

        if held is not None:
            def commit():
                with self._lock:
                    return self._deliver(message_id)

            delivery = await_held(
                held, timeout, commit, EmailTimeout("no answer yet")
            )

        if self.fail_after_effect:
            self.fail_after_effect = False
            raise EmailTimeout("response lost after send")
        if self.server_error_after_effect:
            self.server_error_after_effect = False
            raise EmailServerError("500 after submission")
        return delivery

    def hold_next(self, *, respond_now=False):
        held = HeldCall(respond_now=respond_now)
        with self._lock:
            self._held.append(held)
        return held

    def _deliver(self, message_id):
        delivery_id = "msg_" + uuid4().hex
        self._ledger.record_effect(message_id, delivery_id)
        return delivery_id
