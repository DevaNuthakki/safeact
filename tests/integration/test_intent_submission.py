"""Atomic SafeAct intent-submission integration tests.

All inserted records are rolled back by their owning test.
No provider requests or queue messages are generated.
"""

from dataclasses import replace
import os
import unittest
from unittest import mock
from uuid import uuid4

import sqlalchemy as sa

from core.connector_profiles import PAYMENTS
from storage.database import local_database_url
from storage.schema import (
    audit_events,
    attempts,
    operations,
    outbox_events,
)
from core.provider_keys import derive_provider_key
from storage.submission import (
    IntentSubmission,
    SubmissionConflict,
    SubmissionStateUncertain,
    SubmissionValidationError,
    _immutable_identity,
    _prepare_payload,
    _refuse_skipped_conflict,
    record_intent,
)


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1",
    "Explicit database integration opt-in required",
)
class IntentSubmissionTests(unittest.TestCase):

    def setUp(self):
        self.engine = sa.create_engine(
            local_database_url(),
            connect_args={"connect_timeout": 5},
        )
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()

    def tearDown(self):
        try:
            if self.transaction.is_active:
                self.transaction.rollback()
        finally:
            self.connection.close()
            self.engine.dispose()

    @staticmethod
    def request(**changes):
        base = IntentSubmission(
            tenant_id="synthetic-tenant",
            request_key="synthetic-request",
            action_type="create_case",
            resource_id="synthetic-resource",
            provider_account_id="mock-provider-account",
            connector_id="mock-crm",
            contract_version="v1",
            actor_id="test-actor",
            payload={
                "case": "synthetic",
                "severity": 1,
            },
        )
        return replace(base, **changes)

    def count(self, table):
        return self.connection.execute(
            sa.select(sa.func.count()).select_from(table)
        ).scalar_one()

    def assert_bundle_counts(
        self,
        operations_count,
        audit_count,
        outbox_count,
    ):
        self.assertEqual(
            self.count(operations),
            operations_count,
        )
        self.assertEqual(
            self.count(audit_events),
            audit_count,
        )
        self.assertEqual(
            self.count(outbox_events),
            outbox_count,
        )
        self.assertEqual(self.count(attempts), 0)

    def test_records_operation_audit_and_safe_outbox(self):
        result = record_intent(
            self.connection,
            self.request(),
        )

        self.assertTrue(result.created)
        self.assert_bundle_counts(1, 1, 1)

        operation = self.connection.execute(
            sa.select(operations).where(
                operations.c.operation_id
                == result.operation_id
            )
        ).mappings().one()

        audit = self.connection.execute(
            sa.select(audit_events).where(
                audit_events.c.operation_id
                == result.operation_id
            )
        ).mappings().one()

        outbox = self.connection.execute(
            sa.select(outbox_events).where(
                outbox_events.c.operation_id
                == result.operation_id
            )
        ).mappings().one()

        self.assertEqual(operation["state"], "RECORDED")
        self.assertEqual(operation["version"], 0)
        self.assertEqual(operation["payload_canonical"],
                         '{"case":"synthetic","severity":1}')

        self.assertEqual(
            audit["event_type"],
            "INTENT_RECORDED",
        )
        self.assertFalse(
            audit["details"]["dispatch_authorized"]
        )

        self.assertEqual(
            outbox["event_type"],
            "INTENT_RECORDED_NO_DISPATCH",
        )
        self.assertFalse(
            outbox["payload"]["dispatch_enabled"]
        )
        self.assertEqual(outbox["state"], "PENDING")

    def test_identical_submission_is_idempotent(self):
        request = self.request()

        first = record_intent(self.connection, request)
        second = record_intent(self.connection, request)

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(
            first.operation_id,
            second.operation_id,
        )
        self.assert_bundle_counts(1, 1, 1)

    def test_reordered_payload_keys_reuse_operation(self):
        first = record_intent(
            self.connection,
            self.request(
                payload={"b": 2, "a": 1},
            ),
        )

        second = record_intent(
            self.connection,
            self.request(
                payload={"a": 1, "b": 2},
            ),
        )

        self.assertEqual(
            first.operation_id,
            second.operation_id,
        )
        self.assertFalse(second.created)
        self.assert_bundle_counts(1, 1, 1)

    def test_conflicting_payload_is_rejected(self):
        record_intent(
            self.connection,
            self.request(),
        )

        with self.assertRaises(SubmissionConflict):
            record_intent(
                self.connection,
                self.request(
                    payload={"case": "different"},
                ),
            )

        self.assert_bundle_counts(1, 1, 1)

    def test_other_immutable_identity_changes_conflict(self):
        record_intent(
            self.connection,
            self.request(),
        )

        changes = (
            {"action_type": "delete_case"},
            {"resource_id": "other-resource"},
            {"provider_account_id": "other-account"},
            {"connector_id": "other-connector"},
            {"contract_version": "v2"},
        )

        for change in changes:
            with self.subTest(change=change):
                with self.assertRaises(
                    SubmissionConflict
                ):
                    record_intent(
                        self.connection,
                        self.request(**change),
                    )

        self.assert_bundle_counts(1, 1, 1)

    # -- each unique constraint the admission insert can hit ----------

    def assert_refused(self, error, constraint):
        self.assertEqual(error.sqlstate, "23505")
        self.assertEqual(error.constraint, constraint)

    def test_request_key_conflict_names_its_constraint(self):
        record_intent(self.connection, self.request())

        with self.assertRaises(SubmissionConflict) as caught:
            record_intent(
                self.connection,
                self.request(payload={"case": "different"}),
            )

        self.assert_refused(
            caught.exception, "uq_safeact_operations_tenant_request"
        )
        self.assert_bundle_counts(1, 1, 1)

    def test_native_key_replay_hits_both_indexes_and_returns_row(self):
        # A payments request conflicts on the request key and on the
        # derived provider key index at once; that is still one row.
        request = self.request(connector_id=PAYMENTS)
        first = record_intent(self.connection, request)
        second = record_intent(self.connection, request)

        self.assertIsNotNone(request.provider_idempotency_key)
        self.assertFalse(second.created)
        self.assertEqual(first.operation_id, second.operation_id)
        self.assert_bundle_counts(1, 1, 1)

    def test_provider_key_conflict_from_another_request_is_refused(self):
        # The 0008 CHECK makes this unreachable: the key is derived
        # from the request key. Drop it inside this test's rolled-back
        # transaction only, to prove the insert never mistakes a
        # provider key collision for a replay.
        self.connection.execute(sa.text(
            "ALTER TABLE safeact_operations DROP CONSTRAINT "
            "ck_safeact_operations_provider_key_derived"
        ))
        first = self.request(connector_id=PAYMENTS)
        record_intent(self.connection, first)
        other = self.request(
            connector_id=PAYMENTS, request_key="other-request",
        )
        object.__setattr__(
            other, "provider_idempotency_key",
            first.provider_idempotency_key,
        )

        with self.assertRaises(SubmissionConflict) as caught:
            record_intent(self.connection, other)

        self.assert_refused(
            caught.exception, "uq_safeact_operations_provider_key"
        )
        self.assert_bundle_counts(1, 1, 1)

    def test_primary_key_conflict_is_refused(self):
        first = record_intent(self.connection, self.request())

        with mock.patch(
            "storage.submission.uuid4",
            return_value=first.operation_id,
        ):
            with self.assertRaises(SubmissionConflict) as caught:
                record_intent(
                    self.connection,
                    self.request(request_key="other-request"),
                )

        self.assert_refused(caught.exception, "safeact_operations_pkey")
        self.assert_bundle_counts(1, 1, 1)

    def test_vanished_conflict_never_returns_success(self):
        # If the skipped conflict is gone when PostgreSQL is asked to
        # name it, the probe insert succeeds; it is rolled back and the
        # caller gets uncertainty, never a receipt without a row.
        request = self.request()
        wire, digest = _prepare_payload(request.payload)

        with self.assertRaises(SubmissionStateUncertain) as caught:
            _refuse_skipped_conflict(
                self.connection, uuid4(),
                _immutable_identity(request, wire, digest),
            )

        self.assertIsNone(caught.exception.sqlstate)
        self.assert_bundle_counts(0, 0, 0)

    def test_caller_cannot_choose_a_provider_key(self):
        # Keys are derived server-side from operation identity.
        with self.assertRaises(SubmissionValidationError):
            self.request(provider_idempotency_key="other-key")

        receipt = record_intent(
            self.connection,
            self.request(connector_id="mock-payments"),
        )
        self.assertTrue(receipt.created)
        stored = self.connection.execute(sa.text(
            "SELECT provider_idempotency_key FROM safeact_operations "
            "WHERE operation_id = :id"
        ), {"id": receipt.operation_id}).scalar_one()
        self.assertEqual(stored, derive_provider_key(
            "synthetic-tenant", "synthetic-request",
            "mock-payments", "mock-provider-account",
        ))

    def test_same_request_key_in_other_tenant_is_distinct(self):
        first = record_intent(
            self.connection,
            self.request(),
        )

        second = record_intent(
            self.connection,
            self.request(tenant_id="other-tenant"),
        )

        self.assertNotEqual(
            first.operation_id,
            second.operation_id,
        )
        self.assert_bundle_counts(2, 2, 2)

    def test_float_and_non_object_payloads_are_rejected(self):
        with self.assertRaises(
            SubmissionValidationError
        ):
            record_intent(
                self.connection,
                self.request(payload={"ratio": 1.25}),
            )

        with self.assertRaises(
            SubmissionValidationError
        ):
            self.request(payload=[1, 2])

        self.assert_bundle_counts(0, 0, 0)

    def test_oversized_payload_is_rejected(self):
        with self.assertRaises(
            SubmissionValidationError
        ):
            record_intent(
                self.connection,
                self.request(
                    payload={"blob": "x" * 65536},
                ),
            )

        self.assert_bundle_counts(0, 0, 0)

    def test_uncommitted_bundle_is_invisible_elsewhere(self):
        result = record_intent(
            self.connection,
            self.request(),
        )

        with self.engine.connect() as observer:
            for table in (
                operations,
                audit_events,
                outbox_events,
            ):
                observed = observer.execute(
                    sa.select(
                        sa.func.count()
                    ).select_from(table).where(
                        table.c.operation_id
                        == result.operation_id
                    )
                ).scalar_one()

                self.assertEqual(observed, 0)

        self.assert_bundle_counts(1, 1, 1)

    def test_outer_rollback_removes_entire_bundle(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "injected rollback",
        ):
            with self.connection.begin_nested():
                record_intent(
                    self.connection,
                    self.request(),
                )
                raise RuntimeError(
                    "injected rollback"
                )

        self.assert_bundle_counts(0, 0, 0)

    def test_injected_audit_or_outbox_failure_is_atomic(self):
        for target in (audit_events, outbox_events):
            with self.subTest(target=target.name):

                def inject(
                    connection,
                    clauseelement,
                    multiparams,
                    params,
                    execution_options,
                ):
                    if (
                        getattr(
                            clauseelement,
                            "table",
                            None,
                        ) is target
                    ):
                        raise RuntimeError(
                            "injected write failure"
                        )

                sa.event.listen(
                    self.connection,
                    "before_execute",
                    inject,
                )

                try:
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "injected write failure",
                    ):
                        # record_intent's own savepoint must
                        # roll back the already-inserted rows.
                        record_intent(
                            self.connection,
                            self.request(),
                        )
                finally:
                    sa.event.remove(
                        self.connection,
                        "before_execute",
                        inject,
                    )

                self.assert_bundle_counts(0, 0, 0)

    def test_explicit_outer_transaction_is_required(self):
        with self.engine.connect() as other:
            with self.assertRaises(RuntimeError):
                record_intent(
                    other,
                    self.request(),
                )

        self.assert_bundle_counts(0, 0, 0)


if __name__ == "__main__":
    unittest.main()
