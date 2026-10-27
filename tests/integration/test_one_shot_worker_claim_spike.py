"""PostgreSQL one-shot claim experiment.

This is a test-only candidate reservation protocol.

There is no production claim API and no provider dispatch.
Each test creates and drops its own random isolated schema.

Reservation ownership labels in this experiment are NOT
authenticated worker principals.
"""

import os
import re
import unittest

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import sqlalchemy as sa

from sqlalchemy.pool import NullPool

from storage.database import local_database_url
from storage.schema import (
    metadata,
    operations,
    attempts,
    audit_events,
    outbox_events,
)
from storage.submission import (
    IntentSubmission,
    submit_intent,
)


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit opt-in required for disposable PostgreSQL tests",
)
class OneShotWorkerClaimSpikeTests(unittest.TestCase):

    def setUp(self):
        self.schema_name = "safeact_it_" + uuid4().hex

        if not re.fullmatch(
            r"safeact_it_[0-9a-f]{32}",
            self.schema_name,
        ):
            raise RuntimeError(
                "STOP: Invalid disposable schema name"
            )

        url = local_database_url()

        if (
            url.drivername != "postgresql+psycopg"
            or url.username != "safeact_dev"
            or url.host != "127.0.0.1"
            or url.database != "safeact_dev"
            or url.port is None
        ):
            raise RuntimeError(
                "STOP: Unexpected database destination"
            )

        self.runtime_engine = None

        self.admin_engine = sa.create_engine(
            url,
            poolclass=NullPool,
            connect_args={
                "connect_timeout": 5,
            },
        )

        # Registered before creating the schema so failure
        # during setup still triggers cleanup.
        self.addCleanup(self.cleanup_schema)

        with self.admin_engine.begin() as db:
            self.assert_database_identity(db)

            db.execute(
                sa.text(
                    f'CREATE SCHEMA "{self.schema_name}"'
                )
            )

        self.runtime_engine = sa.create_engine(
            url,
            poolclass=NullPool,
            connect_args={
                "connect_timeout": 5,
                "options": (
                    f"-c search_path={self.schema_name},public"
                ),
            },
        )

        with self.runtime_engine.connect() as db:
            current = db.execute(
                sa.text("SELECT current_schema()")
            ).scalar_one()

            if current != self.schema_name:
                raise RuntimeError(
                    "STOP: Disposable search path missing"
                )

            existing = sa.inspect(db).get_table_names(
                schema=self.schema_name
            )

            if existing:
                raise RuntimeError(
                    "STOP: Disposable schema not empty"
                )

        metadata.create_all(
            self.runtime_engine,
            checkfirst=False,
        )

        with self.runtime_engine.begin() as db:
            created = set(
                sa.inspect(db).get_table_names(
                    schema=self.schema_name
                )
            )

            if created != set(metadata.tables):
                raise RuntimeError(
                    "STOP: Foundation table mismatch"
                )

            for table in metadata.tables:
                if not re.fullmatch(
                    r"[a-z][a-z0-9_]*",
                    table,
                ):
                    raise RuntimeError(
                        "STOP: Unsafe table identifier"
                    )

                db.execute(
                    sa.text(
                        f'CREATE TRIGGER "{table}_write_guard" '
                        f'BEFORE UPDATE OR DELETE '
                        f'ON "{self.schema_name}"."{table}" '
                        f'FOR EACH ROW EXECUTE FUNCTION '
                        f'public.safeact_reject_mutation()'
                    )
                )

            db.execute(
                sa.text(
                    f'CREATE TABLE '
                    f'"{self.schema_name}".'
                    f'"safeact_experimental_claims" ('
                    f'claim_id uuid PRIMARY KEY, '
                    f'operation_id uuid NOT NULL '
                    f'REFERENCES "{self.schema_name}".'
                    f'"safeact_operations"(operation_id) '
                    f'ON DELETE RESTRICT, '
                    f'worker_label text NOT NULL '
                    f'CHECK (length(worker_label) BETWEEN 1 AND 128), '
                    f'dispatch_enabled boolean NOT NULL '
                    f'DEFAULT false '
                    f'CHECK (dispatch_enabled = false), '
                    f'created_at timestamptz NOT NULL '
                    f'DEFAULT now(), '
                    f'UNIQUE (operation_id)'
                    f')'
                )
            )

            db.execute(
                sa.text(
                    f'CREATE TRIGGER '
                    f'"safeact_experimental_claims_write_guard" '
                    f'BEFORE UPDATE OR DELETE '
                    f'ON "{self.schema_name}".'
                    f'"safeact_experimental_claims" '
                    f'FOR EACH ROW EXECUTE FUNCTION '
                    f'public.safeact_reject_mutation()'
                )
            )

        self.claim_table = (
            f'"{self.schema_name}".'
            f'"safeact_experimental_claims"'
        )

        with self.runtime_engine.connect() as db:
            current = db.execute(
                sa.text("SELECT current_schema()")
            ).scalar_one()

            if current != self.schema_name:
                raise RuntimeError(
                    "STOP: Runtime connection escaped schema"
                )

    @staticmethod
    def assert_database_identity(connection):
        result = connection.execute(
            sa.text(
                "SELECT current_database(), current_user"
            )
        ).one()

        if result != ("safeact_dev", "safeact_dev"):
            raise RuntimeError(
                "STOP: Unexpected database or role"
            )

    def cleanup_schema(self):
        if not re.fullmatch(
            r"safeact_it_[0-9a-f]{32}",
            self.schema_name,
        ):
            raise RuntimeError(
                "STOP: Unsafe schema cleanup identifier"
            )

        try:
            if self.runtime_engine is not None:
                self.runtime_engine.dispose()

            with self.admin_engine.begin() as db:
                self.assert_database_identity(db)

                db.execute(
                    sa.text(
                        f'DROP SCHEMA IF EXISTS '
                        f'"{self.schema_name}" CASCADE'
                    )
                )
        finally:
            self.admin_engine.dispose()

    @staticmethod
    def request(**changes):
        original = IntentSubmission(
            tenant_id="synthetic-tenant",
            actor_id="synthetic-actor",
            request_key="one-shot-claim-request",
            action_type="create_case",
            resource_id="synthetic-case",
            provider_account_id="mock-account",
            connector_id="mock-crm",
            contract_version="v1",
            payload={"case_id": "synthetic-case"},
        )

        return replace(original, **changes)

    def admit(self, **changes):
        receipt = submit_intent(
            self.runtime_engine,
            self.request(**changes),
        )

        self.assertTrue(receipt.created)

        return receipt.operation_id

    def claim_on_connection(
        self,
        connection,
        operation_id,
        worker_label,
        *,
        dispatch_enabled=False,
    ):
        return connection.execute(
            sa.text(
                f'INSERT INTO {self.claim_table} '
                f'(claim_id,operation_id,worker_label,'
                f'dispatch_enabled) '
                f'VALUES (:claim_id,:operation_id,'
                f':worker_label,:dispatch_enabled) '
                f'ON CONFLICT (operation_id) '
                f'DO NOTHING '
                f'RETURNING claim_id'
            ),
            {
                "claim_id": uuid4(),
                "operation_id": operation_id,
                "worker_label": worker_label,
                "dispatch_enabled": dispatch_enabled,
            },
        ).scalar_one_or_none()

    def reserve(
        self,
        operation_id,
        worker_label,
        *,
        dispatch_enabled=False,
    ):
        with self.runtime_engine.begin() as db:
            return self.claim_on_connection(
                db,
                operation_id,
                worker_label,
                dispatch_enabled=dispatch_enabled,
            )

    def claim_count(self):
        with self.runtime_engine.connect() as db:
            return db.execute(
                sa.text(
                    f'SELECT count(*) '
                    f'FROM {self.claim_table}'
                )
            ).scalar_one()

    def bundle_counts(self):
        # Order: operations, attempts, audit, outbox.
        with self.runtime_engine.connect() as db:
            return tuple(
                db.execute(
                    sa.select(
                        sa.func.count()
                    ).select_from(table)
                ).scalar_one()
                for table in (
                    operations,
                    attempts,
                    audit_events,
                    outbox_events,
                )
            )

    def test_eight_contenders_have_one_durable_winner(self):
        operation_id = self.admit()
        barrier = Barrier(8)

        def contender(index):
            barrier.wait(timeout=10)

            return self.reserve(
                operation_id,
                f"synthetic-worker-{index}",
            )

        with ThreadPoolExecutor(
            max_workers=8
        ) as executor:
            results = list(
                executor.map(
                    contender,
                    range(8),
                )
            )

        winners = [
            result
            for result in results
            if result is not None
        ]

        self.assertEqual(len(winners), 1)
        self.assertEqual(self.claim_count(), 1)

        self.assertEqual(
            self.bundle_counts(),
            (1, 0, 1, 1),
        )

    def test_repeated_claim_cannot_create_second_winner(self):
        operation_id = self.admit()

        first = self.reserve(
            operation_id,
            "synthetic-worker-alpha",
        )

        second = self.reserve(
            operation_id,
            "synthetic-worker-alpha",
        )

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(self.claim_count(), 1)

        self.assertEqual(
            self.bundle_counts(),
            (1, 0, 1, 1),
        )

    def test_different_operations_are_independent(self):
        first_operation = self.admit()

        second_operation = self.admit(
            request_key="another-request"
        )

        first = self.reserve(
            first_operation,
            "synthetic-worker-alpha",
        )

        second = self.reserve(
            second_operation,
            "synthetic-worker-beta",
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)
        self.assertEqual(self.claim_count(), 2)

        self.assertEqual(
            self.bundle_counts(),
            (2, 0, 2, 2),
        )

    def test_rolled_back_claim_is_not_a_winner(self):
        operation_id = self.admit()

        with self.runtime_engine.connect() as writer:
            transaction = writer.begin()

            try:
                proposed = self.claim_on_connection(
                    writer,
                    operation_id,
                    "synthetic-worker-uncommitted",
                )

                self.assertIsNotNone(proposed)

                # Independent connection must not see an
                # uncommitted reservation.
                self.assertEqual(
                    self.claim_count(),
                    0,
                )

            finally:
                if transaction.is_active:
                    transaction.rollback()

        committed = self.reserve(
            operation_id,
            "synthetic-worker-committed",
        )

        self.assertIsNotNone(committed)
        self.assertEqual(self.claim_count(), 1)

        self.assertEqual(
            self.bundle_counts(),
            (1, 0, 1, 1),
        )

    def test_dispatch_true_is_rejected_by_database(self):
        operation_id = self.admit()

        with self.assertRaises(
            sa.exc.IntegrityError
        ) as caught:
            self.reserve(
                operation_id,
                "synthetic-worker",
                dispatch_enabled=True,
            )

        self.assertEqual(
            getattr(caught.exception.orig, "sqlstate", None),
            "23514",
        )

        self.assertEqual(self.claim_count(), 0)

        self.assertEqual(
            self.bundle_counts(),
            (1, 0, 1, 1),
        )

    def test_nonexistent_operation_is_rejected(self):
        with self.assertRaises(
            sa.exc.IntegrityError
        ) as caught:
            self.reserve(
                uuid4(),
                "synthetic-worker",
            )

        self.assertEqual(
            getattr(caught.exception.orig, "sqlstate", None),
            "23503",
        )

        self.assertEqual(self.claim_count(), 0)

        self.assertEqual(
            self.bundle_counts(),
            (0, 0, 0, 0),
        )

    def test_committed_claim_cannot_be_mutated(self):
        operation_id = self.admit()

        reservation = self.reserve(
            operation_id,
            "synthetic-worker",
        )

        self.assertIsNotNone(reservation)

        with self.assertRaises(
            sa.exc.DBAPIError
        ):
            with self.runtime_engine.begin() as db:
                db.execute(
                    sa.text(
                        f'UPDATE {self.claim_table} '
                        f'SET worker_label = :worker '
                        f'WHERE claim_id = :claim'
                    ),
                    {
                        "worker": "synthetic-takeover",
                        "claim": reservation,
                    },
                )

        self.assertEqual(self.claim_count(), 1)

        with self.runtime_engine.connect() as db:
            row = db.execute(
                sa.text(
                    f'SELECT worker_label, dispatch_enabled '
                    f'FROM {self.claim_table} '
                    f'WHERE claim_id = :claim'
                ),
                {"claim": reservation},
            ).one()

        self.assertEqual(
            row.worker_label,
            "synthetic-worker",
        )

        self.assertIs(row.dispatch_enabled, False)

        self.assertEqual(
            self.bundle_counts(),
            (1, 0, 1, 1),
        )


if __name__ == "__main__":
    unittest.main()
