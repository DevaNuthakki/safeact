"""Isolated PostgreSQL append-only dispatch-risk fence experiment.

This is NOT a production worker, authorization API, or
permission to execute a provider effect.

The reservation table and fence table exist ONLY inside
each test's randomly named disposable PostgreSQL schema.

The risk fence represents MAY_HAVE_DISPATCHED, not proof
of a provider call or completed effect.

No provider adapter or outbound provider call exists here.
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
    "Explicit disposable PostgreSQL test opt-in required",
)
class DispatchRiskFenceSpikeTests(unittest.TestCase):

    def setUp(self):
        self.schema_name = "safeact_it_" + uuid4().hex

        if not re.fullmatch(
            r"safeact_it_[0-9a-f]{32}",
            self.schema_name,
        ):
            raise RuntimeError("STOP: Invalid schema identifier")

        url = local_database_url()

        if (
            url.drivername != "postgresql+psycopg"
            or url.username != "safeact_dev"
            or url.host != "127.0.0.1"
            or url.database != "safeact_dev"
            or url.port is None
        ):
            raise RuntimeError("STOP: Unexpected database target")

        self.runtime_engine = None

        self.admin_engine = sa.create_engine(
            url,
            poolclass=NullPool,
            connect_args={"connect_timeout": 5},
        )

        # Always register cleanup before creating the schema.
        self.addCleanup(self.cleanup_schema)

        with self.admin_engine.begin() as db:
            self.assert_local_database(db)

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
                    "STOP: Disposable search path not active"
                )

            existing = sa.inspect(db).get_table_names(
                schema=self.schema_name
            )

            if existing:
                raise RuntimeError(
                    "STOP: Disposable schema is not empty"
                )

        metadata.create_all(
            self.runtime_engine,
            checkfirst=False,
        )

        s = f'"{self.schema_name}"'

        self.claim_table = (
            f'{s}."safeact_experimental_claims"'
        )

        self.fence_table = (
            f'{s}."safeact_experimental_risk_fences"'
        )

        with self.runtime_engine.begin() as db:
            created = set(
                sa.inspect(db).get_table_names(
                    schema=self.schema_name
                )
            )

            if created != set(metadata.tables):
                raise RuntimeError(
                    "STOP: Foundation table inventory mismatch"
                )

            # Preserve existing append-only write guards.
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
                        f'ON {s}."{table}" '
                        f'FOR EACH ROW EXECUTE FUNCTION '
                        f'public.safeact_reject_mutation()'
                    )
                )

            # A composite FK must verify that the attempt
            # belongs to the SAME operation as the fence.
            # This addition is test-schema-only.
            db.execute(
                sa.text(
                    f'ALTER TABLE {s}."safeact_attempts" '
                    f'ADD CONSTRAINT '
                    f'"safeact_exp_attempt_operation_pair" '
                    f'UNIQUE (operation_id, attempt_id)'
                )
            )

            db.execute(
                sa.text(
                    f'CREATE TABLE {self.claim_table} ('
                    f'claim_id uuid PRIMARY KEY, '
                    f'operation_id uuid NOT NULL '
                    f'REFERENCES {s}."safeact_operations"'
                    f'(operation_id) ON DELETE RESTRICT, '
                    f'worker_label text NOT NULL '
                    f'CHECK (length(worker_label) BETWEEN 1 AND 128), '
                    f'dispatch_enabled boolean NOT NULL '
                    f'DEFAULT false '
                    f'CHECK (dispatch_enabled = false), '
                    f'UNIQUE (operation_id), '
                    f'UNIQUE (operation_id, claim_id)'
                    f')'
                )
            )

            db.execute(
                sa.text(
                    f'CREATE TABLE {self.fence_table} ('
                    f'fence_id uuid PRIMARY KEY, '
                    f'operation_id uuid NOT NULL, '
                    f'claim_id uuid NOT NULL, '
                    f'attempt_id uuid NOT NULL, '
                    f'event_type text NOT NULL '
                    f'CHECK (event_type = '
                    f"'DISPATCH_RISK_COMMITTED'), "
                    f'dispatch_enabled boolean NOT NULL '
                    f'DEFAULT false '
                    f'CHECK (dispatch_enabled = false), '
                    f'created_at timestamptz NOT NULL DEFAULT now(), '
                    f'UNIQUE (operation_id), '
                    f'FOREIGN KEY (operation_id, claim_id) '
                    f'REFERENCES {self.claim_table}'
                    f'(operation_id, claim_id), '
                    f'FOREIGN KEY (operation_id, attempt_id) '
                    f'REFERENCES {s}."safeact_attempts"'
                    f'(operation_id, attempt_id)'
                    f')'
                )
            )

            for table in (
                "safeact_experimental_claims",
                "safeact_experimental_risk_fences",
            ):
                db.execute(
                    sa.text(
                        f'CREATE TRIGGER "{table}_write_guard" '
                        f'BEFORE UPDATE OR DELETE '
                        f'ON {s}."{table}" '
                        f'FOR EACH ROW EXECUTE FUNCTION '
                        f'public.safeact_reject_mutation()'
                    )
                )

        with self.runtime_engine.connect() as db:
            physical = set(
                sa.inspect(db).get_table_names(
                    schema=self.schema_name
                )
            )

            expected = set(metadata.tables) | {
                "safeact_experimental_claims",
                "safeact_experimental_risk_fences",
            }

            if physical != expected:
                raise RuntimeError(
                    "STOP: Experimental schema inventory mismatch"
                )

    @staticmethod
    def assert_local_database(db):
        identity = db.execute(
            sa.text("SELECT current_database(), current_user")
        ).one()

        if identity != ("safeact_dev", "safeact_dev"):
            raise RuntimeError(
                "STOP: Unexpected database or database role"
            )

    def cleanup_schema(self):
        if not re.fullmatch(
            r"safeact_it_[0-9a-f]{32}",
            self.schema_name,
        ):
            raise RuntimeError("STOP: Unsafe cleanup identifier")

        try:
            if self.runtime_engine is not None:
                self.runtime_engine.dispose()

            with self.admin_engine.begin() as db:
                self.assert_local_database(db)

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
            request_key="risk-fence-request",
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

    def prepare_attempt(self, operation_id):
        attempt_id = uuid4()

        with self.runtime_engine.begin() as db:
            db.execute(
                sa.insert(attempts).values(
                    attempt_id=attempt_id,
                    operation_id=operation_id,
                    ordinal=1,
                    dispatch_risk="PREPARED",
                )
            )

        return attempt_id

    def reserve(self, operation_id, label="synthetic-worker"):
        claim_id = uuid4()

        with self.runtime_engine.begin() as db:
            result = db.execute(
                sa.text(
                    f'INSERT INTO {self.claim_table} '
                    f'(claim_id,operation_id,worker_label) '
                    f'VALUES (:claim_id,:operation_id,:label) '
                    f'ON CONFLICT (operation_id) '
                    f'DO NOTHING RETURNING claim_id'
                ),
                {
                    "claim_id": claim_id,
                    "operation_id": operation_id,
                    "label": label,
                },
            ).scalar_one_or_none()

        return result

    def insert_fence(
        self,
        db,
        operation_id,
        claim_id,
        attempt_id,
        *,
        dispatch_enabled=False,
    ):
        return db.execute(
            sa.text(
                f'INSERT INTO {self.fence_table} '
                f'(fence_id,operation_id,claim_id,attempt_id,'
                f'event_type,dispatch_enabled) '
                f'VALUES (:fence_id,:operation_id,:claim_id,'
                f':attempt_id,:event_type,:dispatch_enabled) '
                f'ON CONFLICT (operation_id) '
                f'DO NOTHING RETURNING fence_id'
            ),
            {
                "fence_id": uuid4(),
                "operation_id": operation_id,
                "claim_id": claim_id,
                "attempt_id": attempt_id,
                "event_type": "DISPATCH_RISK_COMMITTED",
                "dispatch_enabled": dispatch_enabled,
            },
        ).scalar_one_or_none()

    def commit_fence(
        self,
        operation_id,
        claim_id,
        attempt_id,
        *,
        dispatch_enabled=False,
    ):
        with self.runtime_engine.begin() as db:
            return self.insert_fence(
                db,
                operation_id,
                claim_id,
                attempt_id,
                dispatch_enabled=dispatch_enabled,
            )

    def ready(self):
        operation_id = self.admit()
        attempt_id = self.prepare_attempt(operation_id)
        claim_id = self.reserve(operation_id)

        self.assertIsNotNone(claim_id)

        return operation_id, claim_id, attempt_id

    def fence_count(self):
        with self.runtime_engine.connect() as db:
            return db.execute(
                sa.text(
                    f'SELECT count(*) FROM {self.fence_table}'
                )
            ).scalar_one()

    def bundle_counts(self):
        # operations, attempts, audit_events, outbox_events
        with self.runtime_engine.connect() as db:
            return tuple(
                db.execute(
                    sa.select(sa.func.count()).select_from(table)
                ).scalar_one()
                for table in (
                    operations,
                    attempts,
                    audit_events,
                    outbox_events,
                )
            )

    def test_committed_fence_binds_claim_attempt_and_operation(self):
        operation_id, claim_id, attempt_id = self.ready()

        fence_id = self.commit_fence(
            operation_id,
            claim_id,
            attempt_id,
        )

        self.assertIsNotNone(fence_id)
        self.assertEqual(self.fence_count(), 1)
        self.assertEqual(
            self.bundle_counts(),
            (1, 1, 1, 1),
        )

        with self.runtime_engine.connect() as db:
            row = db.execute(
                sa.text(
                    f'SELECT operation_id,claim_id,attempt_id,'
                    f'event_type,dispatch_enabled '
                    f'FROM {self.fence_table}'
                )
            ).one()

        self.assertEqual(row.operation_id, operation_id)
        self.assertEqual(row.claim_id, claim_id)
        self.assertEqual(row.attempt_id, attempt_id)
        self.assertEqual(
            row.event_type,
            "DISPATCH_RISK_COMMITTED",
        )
        self.assertIs(row.dispatch_enabled, False)

    def test_eight_contenders_commit_only_one_fence(self):
        operation_id, claim_id, attempt_id = self.ready()
        barrier = Barrier(8)

        def contender(_):
            barrier.wait(timeout=10)

            return self.commit_fence(
                operation_id,
                claim_id,
                attempt_id,
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(contender, range(8)))

        self.assertEqual(
            sum(result is not None for result in results),
            1,
        )
        self.assertEqual(self.fence_count(), 1)
        self.assertEqual(self.bundle_counts(), (1, 1, 1, 1))

    def test_rolled_back_fence_is_not_committed(self):
        operation_id, claim_id, attempt_id = self.ready()

        with self.runtime_engine.connect() as writer:
            transaction = writer.begin()

            try:
                proposed = self.insert_fence(
                    writer,
                    operation_id,
                    claim_id,
                    attempt_id,
                )

                self.assertIsNotNone(proposed)

                # An independent connection cannot see this
                # uncommitted risk fence.
                self.assertEqual(self.fence_count(), 0)
            finally:
                if transaction.is_active:
                    transaction.rollback()

        self.assertEqual(self.fence_count(), 0)

        committed = self.commit_fence(
            operation_id,
            claim_id,
            attempt_id,
        )

        self.assertIsNotNone(committed)
        self.assertEqual(self.fence_count(), 1)

    def test_synthetic_postcommit_ack_loss_does_not_duplicate_fence(self):
        operation_id, claim_id, attempt_id = self.ready()

        with self.assertRaises(ConnectionError):
            with self.runtime_engine.begin() as db:
                written = self.insert_fence(
                    db,
                    operation_id,
                    claim_id,
                    attempt_id,
                )
                self.assertIsNotNone(written)

            # Synthetic exception AFTER successful COMMIT.
            # This does not simulate a real network interruption.
            raise ConnectionError(
                "synthetic postcommit acknowledgment loss"
            )

        self.assertEqual(self.fence_count(), 1)

        duplicate = self.commit_fence(
            operation_id,
            claim_id,
            attempt_id,
        )

        self.assertIsNone(duplicate)
        self.assertEqual(self.fence_count(), 1)
        self.assertEqual(self.bundle_counts(), (1, 1, 1, 1))

    def test_cross_operation_claim_and_attempt_are_rejected(self):
        operation_a, claim_a, attempt_a = self.ready()

        operation_b = self.admit(
            request_key="other-risk-fence-request"
        )
        claim_b = self.reserve(
            operation_b,
            "synthetic-other-worker",
        )
        attempt_b = self.prepare_attempt(operation_b)

        self.assertIsNotNone(claim_b)

        cases = (
            ("wrong_claim", claim_b, attempt_a),
            ("wrong_attempt", claim_a, attempt_b),
        )

        for label, claim, attempt in cases:
            with self.subTest(case=label):
                with self.assertRaises(
                    sa.exc.IntegrityError
                ) as caught:
                    self.commit_fence(
                        operation_a,
                        claim,
                        attempt,
                    )

                self.assertEqual(
                    getattr(
                        caught.exception.orig,
                        "sqlstate",
                        None,
                    ),
                    "23503",
                )

        self.assertEqual(self.fence_count(), 0)
        self.assertEqual(self.bundle_counts(), (2, 2, 2, 2))

    def test_fence_requires_real_claim_and_attempt(self):
        operation_id = self.admit()
        claim_id = uuid4()
        attempt_id = uuid4()

        with self.assertRaises(sa.exc.IntegrityError) as caught:
            self.commit_fence(
                operation_id,
                claim_id,
                attempt_id,
            )

        self.assertEqual(
            getattr(caught.exception.orig, "sqlstate", None),
            "23503",
        )

        actual_claim = self.reserve(operation_id)
        self.assertIsNotNone(actual_claim)

        with self.assertRaises(sa.exc.IntegrityError) as caught:
            self.commit_fence(
                operation_id,
                actual_claim,
                attempt_id,
            )

        self.assertEqual(
            getattr(caught.exception.orig, "sqlstate", None),
            "23503",
        )

        self.assertEqual(self.fence_count(), 0)

    def test_dispatch_enabled_true_is_rejected(self):
        operation_id, claim_id, attempt_id = self.ready()

        with self.assertRaises(sa.exc.IntegrityError) as caught:
            self.commit_fence(
                operation_id,
                claim_id,
                attempt_id,
                dispatch_enabled=True,
            )

        self.assertEqual(
            getattr(caught.exception.orig, "sqlstate", None),
            "23514",
        )

        self.assertEqual(self.fence_count(), 0)

    def test_committed_fence_cannot_be_updated_or_deleted(self):
        operation_id, claim_id, attempt_id = self.ready()

        fence_id = self.commit_fence(
            operation_id,
            claim_id,
            attempt_id,
        )

        self.assertIsNotNone(fence_id)

        for statement in (
            f'UPDATE {self.fence_table} '
            f'SET dispatch_enabled = false '
            f'WHERE fence_id = :fence_id',
            f'DELETE FROM {self.fence_table} '
            f'WHERE fence_id = :fence_id',
        ):
            with self.assertRaises(sa.exc.DBAPIError):
                with self.runtime_engine.begin() as db:
                    db.execute(
                        sa.text(statement),
                        {"fence_id": fence_id},
                    )

        self.assertEqual(self.fence_count(), 1)
        self.assertEqual(self.bundle_counts(), (1, 1, 1, 1))


if __name__ == "__main__":
    unittest.main()
