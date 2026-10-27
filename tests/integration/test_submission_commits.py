"""SafeAct committed-submission integration tests.

Every test creates an independently named PostgreSQL
schema, using the production SQLAlchemy table metadata.

The schema is dropped by unittest cleanup after the test.
No public application tables are modified.

These tests do not simulate provider dispatch.
"""

import os
import re
import time
import unittest
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from dataclasses import replace
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.pool import NullPool

from storage.database import local_database_url
from storage.schema import (
    audit_events,
    attempts,
    metadata,
    operations,
    outbox_events,
)
from storage.submission import (
    IntentSubmission,
    IntentObservationStatus,
    SubmissionConflict,
    SubmissionStateUncertain,
    observe_intent,
    record_intent,
    submit_intent,
)


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Disposable database integration tests require opt-in",
)
class CommittedSubmissionTests(unittest.TestCase):

    def setUp(self):
        self.schema_name = "safeact_it_" + uuid4().hex

        if not re.fullmatch(
            r"safeact_it_[0-9a-f]{32}",
            self.schema_name,
        ):
            raise RuntimeError("Invalid disposable schema name")

        self.runtime_engine = None

        self.admin_engine = sa.create_engine(
            local_database_url(),
            poolclass=NullPool,
            connect_args={"connect_timeout": 5},
        )

        schema = self.schema_name

        with self.admin_engine.begin() as db:
            db.execute(
                sa.text(f'CREATE SCHEMA "{schema}"')
            )

        # unittest runs cleanups even if subsequent
        # setup or test assertions fail.
        self.addCleanup(self.cleanup_schema)

        self.runtime_engine = sa.create_engine(
            local_database_url(),
            poolclass=NullPool,
            connect_args={
                "connect_timeout": 5,
                "options": (
                    f"-c search_path={schema},public"
                ),
            },
        )

        with self.runtime_engine.connect() as db:
            active = db.execute(
                sa.text("SELECT current_schema()")
            ).scalar_one()

        if active != schema:
            raise RuntimeError(
                "STOP: Disposable search path not active"
            )

        # This randomly named schema must start empty.
        # PostgreSQL's search_path also includes public,
        # whose existing tables must not satisfy create_all.
        with self.runtime_engine.connect() as db:
            existing_tables = sa.inspect(db).get_table_names(
                schema=schema
            )

        if existing_tables:
            raise RuntimeError(
                "STOP: Disposable schema is not empty"
            )

        metadata.create_all(
            self.runtime_engine,
            checkfirst=False,
        )

        # Check the physical temporary schema explicitly,
        # rather than allowing search_path-based lookup.
        with self.runtime_engine.connect() as db:
            created_tables = set(
                sa.inspect(db).get_table_names(
                    schema=schema
                )
            )

        if created_tables != set(metadata.tables):
            raise RuntimeError(
                "STOP: Disposable tables were not created"
            )

        # Match the insert-only mutation protection
        # used by the real SafeAct schema.
        with self.runtime_engine.begin() as db:
            for table in metadata.tables:
                db.execute(
                    sa.text(
                        f'CREATE TRIGGER "{table}_write_guard" '
                        f'BEFORE UPDATE OR DELETE '
                        f'ON "{schema}"."{table}" '
                        f'FOR EACH ROW EXECUTE FUNCTION '
                        f'public.safeact_reject_mutation()'
                    )
                )

        with self.runtime_engine.connect() as db:
            active = db.execute(
                sa.text("SELECT current_schema()")
            ).scalar_one()

            if active != schema:
                raise RuntimeError(
                    "STOP: Test connection escaped its schema"
                )

        inspector = sa.inspect(self.runtime_engine)

        if set(
            inspector.get_table_names(
                schema=self.schema_name
            )
        ) != set(
            metadata.tables
        ):
            raise RuntimeError(
                "STOP: Disposable schema table mismatch"
            )

    def cleanup_schema(self):
        if self.runtime_engine is not None:
            self.runtime_engine.dispose()

        try:
            if not re.fullmatch(
                r"safeact_it_[0-9a-f]{32}",
                self.schema_name,
            ):
                raise RuntimeError(
                    "Unsafe disposable schema identifier"
                )

            with self.admin_engine.begin() as db:
                db.execute(
                    sa.text(
                        f'DROP SCHEMA '
                        f'"{self.schema_name}" CASCADE'
                    )
                )
        finally:
            self.admin_engine.dispose()

    @staticmethod
    def request(**changes):
        original = IntentSubmission(
            tenant_id="synthetic-tenant",
            request_key="synthetic-commit-request",
            action_type="create_case",
            resource_id="synthetic-resource",
            provider_account_id="mock-account",
            connector_id="mock-crm",
            contract_version="v1",
            actor_id="integration-test",
            payload={
                "case": "synthetic",
                "priority": 1,
            },
        )

        return replace(original, **changes)

    def counts(self):
        with self.runtime_engine.connect() as db:
            return tuple(
                db.execute(
                    sa.select(
                        sa.func.count()
                    ).select_from(table)
                ).scalar_one()
                for table in (
                    operations,
                    audit_events,
                    outbox_events,
                    attempts,
                )
            )

    def test_engine_submission_commits_complete_bundle(self):
        receipt = submit_intent(
            self.runtime_engine,
            self.request(),
        )

        self.assertTrue(receipt.created)
        self.assertEqual(self.counts(), (1, 1, 1, 0))

        with self.runtime_engine.connect() as db:
            state = db.execute(
                sa.select(
                    operations.c.state,
                    operations.c.version,
                )
            ).one()

            event = db.execute(
                sa.select(
                    outbox_events.c.event_type,
                    outbox_events.c.payload,
                )
            ).one()

        self.assertEqual(state, ("RECORDED", 0))
        self.assertEqual(
            event.event_type,
            "INTENT_RECORDED_NO_DISPATCH",
        )
        self.assertFalse(
            event.payload["dispatch_enabled"]
        )

    def test_duplicate_after_commit_returns_same_operation(self):
        request = self.request()

        first = submit_intent(
            self.runtime_engine,
            request,
        )

        second = submit_intent(
            self.runtime_engine,
            request,
        )

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(
            first.operation_id,
            second.operation_id,
        )
        self.assertEqual(self.counts(), (1, 1, 1, 0))

    def test_conflict_after_commit_is_rejected(self):
        first = submit_intent(
            self.runtime_engine,
            self.request(),
        )

        with self.assertRaises(SubmissionConflict):
            submit_intent(
                self.runtime_engine,
                self.request(
                    payload={"case": "different"},
                ),
            )

        self.assertTrue(first.created)
        self.assertEqual(self.counts(), (1, 1, 1, 0))

    def test_different_tenant_can_use_same_request_key(self):
        first = submit_intent(
            self.runtime_engine,
            self.request(),
        )

        second = submit_intent(
            self.runtime_engine,
            self.request(tenant_id="other-tenant"),
        )

        self.assertNotEqual(
            first.operation_id,
            second.operation_id,
        )
        self.assertEqual(self.counts(), (2, 2, 2, 0))

    def test_failure_before_commit_leaves_no_bundle(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "synthetic failure before commit",
        ):
            with self.runtime_engine.begin() as db:
                record_intent(
                    db,
                    self.request(),
                )
                raise RuntimeError(
                    "synthetic failure before commit"
                )

        self.assertEqual(self.counts(), (0, 0, 0, 0))

        retry = submit_intent(
            self.runtime_engine,
            self.request(),
        )

        self.assertTrue(retry.created)
        self.assertEqual(self.counts(), (1, 1, 1, 0))

    def test_simulated_lost_response_after_commit(self):
        request = self.request()

        # This simulates a client losing the response
        # AFTER the database commit has succeeded.
        # It is not a true lost-commit-ack experiment.
        first = submit_intent(
            self.runtime_engine,
            request,
        )

        with self.assertRaises(ConnectionError):
            raise ConnectionError(
                "synthetic response lost after commit"
            )

        resolved = submit_intent(
            self.runtime_engine,
            request,
        )

        self.assertFalse(resolved.created)
        self.assertEqual(
            resolved.operation_id,
            first.operation_id,
        )
        self.assertEqual(self.counts(), (1, 1, 1, 0))


    def _exercise_lock_contention(
        self,
        competing_request,
        *,
        first_commits,
    ):
        """Exercise real unique-index contention in PostgreSQL.

        The first writer holds an uncommitted operation.
        A second independent connection attempts the same
        tenant/request identity.

        We require PostgreSQL to report the blocking PID
        before committing or rolling back the first writer.
        """
        started = Queue(maxsize=1)

        first_connection = self.runtime_engine.connect()
        first_transaction = first_connection.begin()

        try:
            first_receipt = record_intent(
                first_connection,
                self.request(),
            )

            first_pid = first_connection.execute(
                sa.text("SELECT pg_backend_pid()")
            ).scalar_one()

            def second_writer():
                try:
                    with self.runtime_engine.begin() as db:
                        db.execute(
                            sa.text(
                                "SET LOCAL statement_timeout "
                                "= '12000ms'"
                            )
                        )

                        second_pid = db.execute(
                            sa.text("SELECT pg_backend_pid()")
                        ).scalar_one()

                        started.put(second_pid)

                        return record_intent(
                            db,
                            competing_request,
                        )
                except Exception as error:
                    return error

            with ThreadPoolExecutor(
                max_workers=1
            ) as executor:
                future = executor.submit(second_writer)

                try:
                    second_pid = started.get(timeout=5)

                    if second_pid == first_pid:
                        raise AssertionError(
                            "Writers share a PostgreSQL backend"
                        )

                    deadline = time.monotonic() + 8
                    blocked = False

                    with self.admin_engine.connect() as observer:
                        while time.monotonic() < deadline:
                            blockers = observer.execute(
                                sa.text(
                                    "SELECT pg_blocking_pids(:pid)"
                                ),
                                {"pid": second_pid},
                            ).scalar_one()

                            if first_pid in blockers:
                                blocked = True
                                break

                            if future.done():
                                break

                            time.sleep(0.05)

                    if not blocked:
                        if future.done():
                            raise AssertionError(
                                "Competing submission finished "
                                "before lock contention: "
                                f"{future.result()!r}"
                            )

                        raise AssertionError(
                            "Expected PostgreSQL lock "
                            "contention was not observed"
                        )

                    self.assertFalse(future.done())

                    if first_commits:
                        first_transaction.commit()
                    else:
                        first_transaction.rollback()

                    outcome = future.result(timeout=15)

                finally:
                    # Always release the first transaction,
                    # including if an assertion or timeout fails.
                    if first_transaction.is_active:
                        first_transaction.rollback()

            return first_receipt, outcome

        finally:
            if first_transaction.is_active:
                first_transaction.rollback()

            first_connection.close()

    def test_concurrent_identical_requests_commit_one_bundle(self):
        first, second = self._exercise_lock_contention(
            self.request(),
            first_commits=True,
        )

        self.assertTrue(first.created)
        self.assertFalse(
            isinstance(second, Exception),
            repr(second),
        )
        self.assertFalse(second.created)
        self.assertEqual(
            first.operation_id,
            second.operation_id,
        )
        self.assertEqual(self.counts(), (1, 1, 1, 0))

    def test_concurrent_conflicting_request_is_rejected(self):
        first, second = self._exercise_lock_contention(
            self.request(
                payload={"case": "different"},
            ),
            first_commits=True,
        )

        self.assertTrue(first.created)
        self.assertIsInstance(
            second,
            SubmissionConflict,
        )
        self.assertEqual(self.counts(), (1, 1, 1, 0))

    def test_concurrent_rollback_allows_waiter_to_create(self):
        first, second = self._exercise_lock_contention(
            self.request(),
            first_commits=False,
        )

        self.assertTrue(first.created)
        self.assertFalse(
            isinstance(second, Exception),
            repr(second),
        )
        self.assertTrue(second.created)

        self.assertNotEqual(
            first.operation_id,
            second.operation_id,
        )

        self.assertEqual(self.counts(), (1, 1, 1, 0))


    def test_observe_missing_request_is_unknown(self):
        observation = observe_intent(
            self.runtime_engine,
            self.request(),
        )

        self.assertEqual(
            observation.status,
            IntentObservationStatus.UNKNOWN,
        )
        self.assertIsNone(observation.operation_id)
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_observe_committed_bundle(self):
        request = self.request()

        receipt = submit_intent(
            self.runtime_engine,
            request,
        )

        observation = observe_intent(
            self.runtime_engine,
            request,
        )

        self.assertEqual(
            observation.status,
            IntentObservationStatus.CONFIRMED_RECORDED,
        )
        self.assertEqual(
            observation.operation_id,
            receipt.operation_id,
        )
        self.assertEqual(self.counts(), (1, 1, 1, 0))

    def test_observe_conflicting_identity_raises(self):
        submit_intent(
            self.runtime_engine,
            self.request(),
        )

        with self.assertRaises(SubmissionConflict):
            observe_intent(
                self.runtime_engine,
                self.request(
                    payload={"case": "other"},
                ),
            )

        self.assertEqual(self.counts(), (1, 1, 1, 0))

    def test_injected_lost_ack_after_real_commit(self):
        real_engine = self.runtime_engine

        class LoseAckAfterCommit:
            @contextmanager
            def begin(self):
                with real_engine.begin() as connection:
                    yield connection

                # The underlying COMMIT has already succeeded.
                raise ConnectionError(
                    "synthetic lost commit acknowledgement"
                )

        request = self.request()

        with self.assertRaises(ConnectionError):
            submit_intent(
                LoseAckAfterCommit(),
                request,
            )

        observation = observe_intent(
            real_engine,
            request,
        )

        self.assertEqual(
            observation.status,
            IntentObservationStatus.CONFIRMED_RECORDED,
        )
        self.assertEqual(self.counts(), (1, 1, 1, 0))

    def test_injected_failure_before_commit_is_unknown(self):
        real_engine = self.runtime_engine

        class LoseConnectionBeforeCommit:
            @contextmanager
            def begin(self):
                with real_engine.begin() as connection:
                    yield connection
                    raise ConnectionError(
                        "synthetic precommit connection failure"
                    )

        request = self.request()

        with self.assertRaises(ConnectionError):
            submit_intent(
                LoseConnectionBeforeCommit(),
                request,
            )

        observation = observe_intent(
            real_engine,
            request,
        )

        self.assertEqual(
            observation.status,
            IntentObservationStatus.UNKNOWN,
        )
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_inflight_commit_not_found_stays_unknown(self):
        request = self.request()

        with self.runtime_engine.connect() as writer:
            transaction = writer.begin()

            try:
                receipt = record_intent(
                    writer,
                    request,
                )

                before = observe_intent(
                    self.runtime_engine,
                    request,
                )

                self.assertEqual(
                    before.status,
                    IntentObservationStatus.UNKNOWN,
                )

                transaction.commit()
            finally:
                if transaction.is_active:
                    transaction.rollback()

        after = observe_intent(
            self.runtime_engine,
            request,
        )

        self.assertEqual(
            after.status,
            IntentObservationStatus.CONFIRMED_RECORDED,
        )
        self.assertEqual(
            after.operation_id,
            receipt.operation_id,
        )
        self.assertEqual(self.counts(), (1, 1, 1, 0))

    def test_incomplete_committed_bundle_stays_unknown(self):
        # Deliberately bypass the application service in
        # a disposable schema to inject invalid DB state.
        # The current schema permits individual INSERTs.
        request = self.request()
        wire = '{"case":"synthetic","priority":1}'

        from hashlib import sha256

        with self.runtime_engine.begin() as db:
            db.execute(
                sa.insert(operations).values(
                    operation_id=uuid4(),
                    tenant_id=request.tenant_id,
                    request_key=request.request_key,
                    action_type=request.action_type,
                    resource_id=request.resource_id,
                    provider_account_id=(
                        request.provider_account_id
                    ),
                    connector_id=request.connector_id,
                    contract_version=request.contract_version,
                    payload_sha256=sha256(
                        wire.encode("utf-8")
                    ).hexdigest(),
                    payload_canonical=wire,
                    provider_idempotency_key=(
                        request.provider_idempotency_key
                    ),
                )
            )

        observation = observe_intent(
            self.runtime_engine,
            request,
        )

        self.assertEqual(
            observation.status,
            IntentObservationStatus.UNKNOWN,
        )
        self.assertIsNone(observation.operation_id)

        with self.assertRaises(SubmissionStateUncertain):
            submit_intent(
                self.runtime_engine,
                request,
            )

        self.assertEqual(self.counts(), (1, 0, 0, 0))

    def test_observation_read_failure_is_not_confirmed(self):
        def deny_read(
            connection,
            clauseelement,
            multiparams,
            params,
            execution_options,
        ):
            if isinstance(clauseelement, sa.sql.Select):
                raise sa.exc.OperationalError(
                    "synthetic read",
                    {},
                    Exception("injected read failure"),
                )

        sa.event.listen(
            self.runtime_engine,
            "before_execute",
            deny_read,
        )

        try:
            with self.assertRaises(
                SubmissionStateUncertain
            ):
                observe_intent(
                    self.runtime_engine,
                    self.request(),
                )
        finally:
            sa.event.remove(
                self.runtime_engine,
                "before_execute",
                deny_read,
            )

        self.assertEqual(self.counts(), (0, 0, 0, 0))


    def test_partial_initial_events_block_duplicate_replay(self):
        from core.contracts import canonical_payload_bytes
        from hashlib import sha256

        for present in ("audit", "outbox"):
            with self.subTest(present=present):
                request = self.request(
                    request_key=f"partial-{present}"
                )

                wire = canonical_payload_bytes(
                    request.payload
                ).decode("utf-8")

                operation_id = uuid4()

                with self.runtime_engine.begin() as db:
                    db.execute(
                        sa.insert(operations).values(
                            operation_id=operation_id,
                            tenant_id=request.tenant_id,
                            request_key=request.request_key,
                            action_type=request.action_type,
                            resource_id=request.resource_id,
                            provider_account_id=(
                                request.provider_account_id
                            ),
                            connector_id=request.connector_id,
                            contract_version=request.contract_version,
                            payload_sha256=sha256(
                                wire.encode("utf-8")
                            ).hexdigest(),
                            payload_canonical=wire,
                            provider_idempotency_key=(
                                request.provider_idempotency_key
                            ),
                        )
                    )

                    if present == "audit":
                        db.execute(
                            sa.insert(audit_events).values(
                                operation_id=operation_id,
                                event_type="INTENT_RECORDED",
                                actor_id=request.actor_id,
                                details={
                                    "schema_version": 1,
                                    "dispatch_authorized": False,
                                },
                            )
                        )
                    else:
                        db.execute(
                            sa.insert(outbox_events).values(
                                outbox_id=uuid4(),
                                operation_id=operation_id,
                                event_type=(
                                    "INTENT_RECORDED_NO_DISPATCH"
                                ),
                                payload={
                                    "dispatch_enabled": False,
                                },
                            )
                        )

                observation = observe_intent(
                    self.runtime_engine,
                    request,
                )

                self.assertEqual(
                    observation.status,
                    IntentObservationStatus.UNKNOWN,
                )

                with self.assertRaises(
                    SubmissionStateUncertain
                ):
                    submit_intent(
                        self.runtime_engine,
                        request,
                    )

        self.assertEqual(self.counts(), (2, 1, 1, 0))


if __name__ == "__main__":
    unittest.main()
