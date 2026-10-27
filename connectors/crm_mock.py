"""In-process CRM provider mock (a test double, never a provider).

Provider behaviour modelled (CRM profile):
- no native idempotency: every accepted request creates a new record,
  so a blind repeat after an ambiguous dispatch duplicates the effect;
- each record carries the caller's ``external_ref`` (SafeAct sends the
  operation id), which correlates but does not deduplicate;
- search by external_ref is eventually consistent and may be
  incomplete: new records can stay out of the index, and a record can
  be missing from it entirely;
- a validation rejection happens before execution and applies nothing.

Every call and record is appended to the harness's EffectsLedger; the
search answers only from the mock's own index.
"""

from dataclasses import dataclass
from threading import Lock
from uuid import uuid4

from connectors.mock_support import HeldCall, await_held


class CrmTimeout(Exception):
    """No response. The record may or may not exist."""


class CrmServerError(Exception):
    """A 5xx-style response. It says nothing about the record."""


class CrmValidationRejected(Exception):
    """Rejected before execution: no record was created."""

    def __init__(self, reference, *, retryable=False):
        super().__init__(f"record rejected: {reference}")
        self.reference = reference
        self.retryable = retryable


class CrmSearchUnavailable(Exception):
    """The search endpoint could not answer."""


class CrmRateLimited(Exception):
    """429. The CRM documents nothing about it: the record may exist."""

    def __init__(self, retry_after=None):
        super().__init__("rate limited")
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class CrmRecord:
    record_id: str
    external_ref: str
    params: dict


class MockCrmProvider:

    def __init__(self, *, ledger):
        self._ledger = ledger
        self._lock = Lock()
        self._records = []
        self._unindexed = set()   # record ids not yet searchable
        self._lost = set()        # record ids the index never gets
        self._pending = set()     # external refs with a held request
        self._held = []

        # Fault switches. One-shot unless stated otherwise.
        self.fail_before_effect = False
        self.fail_after_effect = False
        self.server_error_after_effect = False
        self.reject_next = None
        self.reject_next_retryable = None
        self.search_unavailable = False
        self.index_lag = False        # persistent: new records unindexed
        self.drop_from_index = False  # one-shot: next record never indexed
        self.corrupt_search = False   # persistent: answers with wrong data
        # F16: the next N requests are rate limited; outage: everything
        # fails (no response, nothing created; search unavailable).
        self.rate_limit_next = 0
        self.retry_after = None
        self.outage = False

    # -- provider surface ----------------------------------------------

    def create_record(self, external_ref, params, *, timeout=None):
        if type(external_ref) is not str or not external_ref:
            raise ValueError("external_ref required")
        if type(params) is not dict:
            raise TypeError("record parameters must be a dict")

        with self._lock:
            self._ledger.record_call(external_ref, params)

            if self.outage:
                raise CrmTimeout("provider down, nothing created")

            if self.rate_limit_next:
                self.rate_limit_next -= 1
                raise CrmRateLimited(self.retry_after)

            if self.fail_before_effect:
                self.fail_before_effect = False
                raise CrmTimeout("no response, nothing created")

            if self.reject_next is not None:
                reference, self.reject_next = self.reject_next, None
                raise CrmValidationRejected(reference)

            if self.reject_next_retryable is not None:
                reference = self.reject_next_retryable
                self.reject_next_retryable = None
                # e.g. "record locked, not processed": nothing created.
                raise CrmValidationRejected(reference, retryable=True)

            held = self._held.pop(0) if self._held else None
            if held is None:
                record = self._create(external_ref, params)
            else:
                self._pending.add(external_ref)

        if held is not None:
            def commit():
                with self._lock:
                    self._pending.discard(external_ref)
                    return self._create(external_ref, params)

            record = await_held(
                held, timeout, commit, CrmTimeout("no answer yet")
            )

        if self.fail_after_effect:
            self.fail_after_effect = False
            raise CrmTimeout("response lost after create")
        if self.server_error_after_effect:
            self.server_error_after_effect = False
            raise CrmServerError("503 after create")
        return record

    def search(self, external_ref):
        """Indexed records with this external_ref (possibly stale)."""

        with self._lock:
            if self.search_unavailable or self.outage:
                raise CrmSearchUnavailable(external_ref)

            found = tuple(
                r for r in self._records
                if r.external_ref == external_ref
                and r.record_id not in self._unindexed
            )
            if self.corrupt_search and found:
                first = found[0]
                return (CrmRecord(
                    first.record_id, external_ref,
                    {**first.params, "corrupted": True},
                ),)
            return found

    # -- test controls -------------------------------------------------

    def hold_next(self, *, respond_now=False):
        held = HeldCall(respond_now=respond_now)
        with self._lock:
            self._held.append(held)
        return held

    def reindex(self):
        """The search index catches up (lagging records only)."""

        with self._lock:
            self._unindexed = set(self._lost)

    # -- internals -----------------------------------------------------

    def _create(self, external_ref, params):
        record_id = "crm_" + uuid4().hex
        record = CrmRecord(record_id, external_ref, dict(params))
        self._records.append(record)
        self._ledger.record_effect(external_ref, record_id)

        if self.drop_from_index:
            self.drop_from_index = False
            self._lost.add(record_id)
            self._unindexed.add(record_id)
        elif self.index_lag:
            self._unindexed.add(record_id)
        return record
