"""Refund-specific mock faults. The initial workflow charge is unaffected."""

import threading

from chaos.recovery_faults import ShortRetentionPayments
from connectors.payments_mock import ProviderTimeout


MODES = frozenset({"lost_response", "hidden_response", "rejected", "order_unknown", "control"})


class WorkflowPayments(ShortRetentionPayments):
    def __init__(self, *, ledger):
        super().__init__(ledger=ledger)
        self._workflow_lock = threading.Lock()
        self.reset_workflow_fault()

    def reset_workflow_fault(self):
        with self._workflow_lock:
            self.workflow_mode = None
            self._refunds_seen = 0
            self._before_refund = None
            self._inverse_lookup_outage = False

    def configure_inverse_lookup_outage(self):
        with self._workflow_lock:
            if self.workflow_mode not in ('lost_response', 'hidden_response'):
                raise ValueError('Inverse lookup fault requires an uncertain refund')
            self._inverse_lookup_outage = True

    def configure_workflow(self, mode, before_refund):
        if mode not in MODES or not callable(before_refund):
            raise ValueError("Invalid workflow fault configuration")
        with self._workflow_lock:
            self.workflow_mode = mode
            self._refunds_seen = 0
            self._before_refund = before_refund

    def refund(self, key, params, *, timeout=None):
        with self._workflow_lock:
            mode, before = self.workflow_mode, self._before_refund
            first = self._refunds_seen == 0
            lookup_outage = self._inverse_lookup_outage
            self._refunds_seen += 1
        if before is not None:
            before()  # Separate committed DB query, never supplied to the connector.
        if lookup_outage:
            self.lookup_unavailable = True
        if mode == "hidden_response":
            self.register_key(key, "q-v2-eventual")
            self.hide_new_effects = True
        if mode == "rejected":
            self.reject_next = "refund_window_closed"
        receipt = super().refund(key, params, timeout=timeout)
        # Drop replay replies too in the hidden case. The native key still
        # protects execution: this affects only what the caller receives.
        if mode == "hidden_response" or (mode == "lost_response" and first):
            raise ProviderTimeout("workflow refund reply lost after application")
        return receipt


def configure(slot, arm, mode):
    if mode not in MODES:
        raise ValueError("Unknown workflow fault mode")
    slot.mocks["payments"].configure_workflow(mode, arm.sample_workflows_before_refund)
    crm = slot.mocks["crm"]
    if mode == "order_unknown":
        crm.fail_after_effect = True
        crm.drop_from_index = True
    elif mode != "control":
        crm.reject_next = "case_closed"
