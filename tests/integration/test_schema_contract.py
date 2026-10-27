"""PostgreSQL foundation contract tests.

These tests intentionally use real PostgreSQL constraints.

Each ordinary test opens a transaction that is always
rolled back. Fault-injection tests use separate connections
and explicitly verify rollback.

No provider dispatch is performed.
"""

import json
import os
import unittest
from uuid import uuid4

from core.contracts import canonical_payload_hash

import psycopg
from psycopg.types.json import Jsonb

from tests.integration.test_postgres_smoke import (
    database_settings,
)


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1",
    "Explicit database integration opt-in required",
)
class FoundationSchemaTests(unittest.TestCase):

    def setUp(self):
        self.db = psycopg.connect(**database_settings())

        if self.db.autocommit:
            self.db.close()
            raise RuntimeError(
                "Unsafe integration-test autocommit configuration"
            )

        # Begin the outer transaction. The rejection helper
        # uses nested savepoints, never separate commits.
        self.db.execute("SELECT 1")

    def tearDown(self):
        try:
            self.db.rollback()
        finally:
            self.db.close()

    def operation(
        self,
        *,
        database=None,
        tenant="tenant-alpha",
        request_key=None,
        digest=None,
        payload=None,
        state="RECORDED",
        version=0,
    ):
        db = database if database is not None else self.db
        operation_id = uuid4()

        if request_key is None:
            request_key = uuid4().hex

        if payload is None:
            payload = {"resource": "resource-1"}

        wire = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

        if digest is None:
            digest = canonical_payload_hash(payload)

        db.execute(
            """
            INSERT INTO public.safeact_operations (
                operation_id,
                tenant_id,
                request_key,
                action_type,
                resource_id,
                provider_account_id,
                connector_id,
                contract_version,
                payload_sha256,
                payload_canonical,
                state,
                version
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                operation_id,
                tenant,
                request_key,
                "create_case",
                "resource-1",
                "provider-account-1",
                "crm-mock",
                "v1",
                digest,
                wire,
                state,
                version,
            ),
        )

        return operation_id

    def attempt(
        self,
        operation_id,
        *,
        database=None,
        ordinal=1,
        risk="PREPARED",
    ):
        db = database if database is not None else self.db
        attempt_id = uuid4()

        db.execute(
            """
            INSERT INTO public.safeact_attempts (
                attempt_id,
                operation_id,
                ordinal,
                dispatch_risk
            )
            VALUES (%s, %s, %s, %s)
            """,
            (attempt_id, operation_id, ordinal, risk),
        )

        return attempt_id

    def audit(self, operation_id, *, database=None):
        db = database if database is not None else self.db

        return db.execute(
            """
            INSERT INTO public.safeact_audit_events (
                operation_id,
                event_type,
                actor_id,
                details
            )
            VALUES (%s, %s, %s, %s)
            RETURNING audit_id
            """,
            (
                operation_id,
                "OPERATION_RECORDED",
                "integration-test",
                Jsonb({"origin": "contract-test"}),
            ),
        ).fetchone()[0]

    def outbox(
        self,
        operation_id,
        *,
        database=None,
        state="PENDING",
    ):
        db = database if database is not None else self.db
        outbox_id = uuid4()

        db.execute(
            """
            INSERT INTO public.safeact_outbox_events (
                outbox_id,
                operation_id,
                event_type,
                state,
                payload
            )
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                outbox_id,
                operation_id,
                "INTENT_RECORDED",
                state,
                Jsonb({"dispatch_enabled": False}),
            ),
        )

        return outbox_id

    def reject(self, action, expected_sqlstate):
        """Assert rejection without poisoning outer transaction."""
        with self.assertRaises(psycopg.Error) as captured:
            with self.db.transaction():
                action()

        self.assertEqual(
            captured.exception.sqlstate,
            expected_sqlstate,
        )

    def test_valid_initial_records(self):
        operation_id = self.operation()
        attempt_id = self.attempt(operation_id)
        self.audit(operation_id)
        self.outbox(operation_id)

        state = self.db.execute(
            """
            SELECT state, version
            FROM public.safeact_operations
            WHERE operation_id = %s
            """,
            (operation_id,),
        ).fetchone()

        risk = self.db.execute(
            """
            SELECT dispatch_risk
            FROM public.safeact_attempts
            WHERE attempt_id = %s
            """,
            (attempt_id,),
        ).fetchone()[0]

        self.assertEqual(state, ("RECORDED", 0))
        self.assertEqual(risk, "PREPARED")

    def test_duplicate_tenant_request_is_rejected(self):
        self.operation(request_key="same-request")

        self.reject(
            lambda: self.operation(request_key="same-request"),
            "23505",
        )

    def test_identical_request_key_in_other_tenant_is_allowed(self):
        self.operation(
            tenant="tenant-alpha",
            request_key="shared-request",
        )

        self.operation(
            tenant="tenant-beta",
            request_key="shared-request",
        )

    def test_changed_payload_cannot_reuse_request_key(self):
        self.operation(
            request_key="same-request",
            payload={"case": "first"},
        )

        self.reject(
            lambda: self.operation(
                request_key="same-request",
                payload={"case": "second"},
            ),
            "23505",
        )

    def test_invalid_operation_fields_are_rejected(self):
        cases = (
            {"tenant": ""},
            {"digest": "not-a-sha256-digest"},
            {"digest": "A" * 64},
            {"state": "READY_TO_DISPATCH"},
            {"version": 1},
        )

        for values in cases:
            with self.subTest(values=values):
                self.reject(
                    lambda values=values: self.operation(**values),
                    "23514",
                )

    def test_attempt_ordinal_must_be_positive(self):
        operation_id = self.operation()

        for ordinal in (0, -1):
            with self.subTest(ordinal=ordinal):
                self.reject(
                    lambda ordinal=ordinal: self.attempt(
                        operation_id,
                        ordinal=ordinal,
                    ),
                    "23514",
                )

    def test_duplicate_attempt_ordinal_is_rejected(self):
        operation_id = self.operation()
        self.attempt(operation_id, ordinal=1)

        self.reject(
            lambda: self.attempt(operation_id, ordinal=1),
            "23505",
        )

    def test_attempt_cannot_start_dispatched(self):
        operation_id = self.operation()

        self.reject(
            lambda: self.attempt(
                operation_id,
                risk="MAY_HAVE_DISPATCHED",
            ),
            "23514",
        )

    def test_attempt_foreign_key_is_enforced(self):
        self.reject(
            lambda: self.attempt(uuid4()),
            "23503",
        )

    def test_audit_foreign_key_is_enforced(self):
        self.reject(
            lambda: self.audit(uuid4()),
            "23503",
        )

    def test_outbox_foreign_key_is_enforced(self):
        self.reject(
            lambda: self.outbox(uuid4()),
            "23503",
        )

    def test_outbox_cannot_start_dispatched(self):
        operation_id = self.operation()

        self.reject(
            lambda: self.outbox(
                operation_id,
                state="DELIVERED",
            ),
            "23514",
        )

    def test_all_four_tables_reject_update_and_delete(self):
        operation_id = self.operation()
        attempt_id = self.attempt(operation_id)
        audit_id = self.audit(operation_id)
        outbox_id = self.outbox(operation_id)

        targets = (
            (
                "safeact_operations",
                "operation_id",
                operation_id,
                "state",
            ),
            (
                "safeact_attempts",
                "attempt_id",
                attempt_id,
                "dispatch_risk",
            ),
            (
                "safeact_audit_events",
                "audit_id",
                audit_id,
                "event_type",
            ),
            (
                "safeact_outbox_events",
                "outbox_id",
                outbox_id,
                "state",
            ),
        )

        for table, key, value, column in targets:
            with self.subTest(table=table, action="UPDATE"):
                self.reject(
                    lambda table=table, key=key,
                           value=value, column=column:
                    self.db.execute(
                        f"""
                        UPDATE public.{table}
                        SET {column} = {column}
                        WHERE {key} = %s
                        """,
                        (value,),
                    ),
                    "23514",
                )

            with self.subTest(table=table, action="DELETE"):
                self.reject(
                    lambda table=table, key=key, value=value:
                    self.db.execute(
                        f"""
                        DELETE FROM public.{table}
                        WHERE {key} = %s
                        """,
                        (value,),
                    ),
                    "23514",
                )

    def test_uncommitted_bundle_is_invisible_to_other_connection(self):
        operation_id = self.operation()
        self.audit(operation_id)
        self.outbox(operation_id)

        with psycopg.connect(**database_settings()) as observer:
            for table in (
                "safeact_operations",
                "safeact_audit_events",
                "safeact_outbox_events",
            ):
                count = observer.execute(
                    f"""
                    SELECT count(*)
                    FROM public.{table}
                    WHERE operation_id = %s
                    """,
                    (operation_id,),
                ).fetchone()[0]

                self.assertEqual(count, 0, table)

    def test_exception_rolls_back_entire_bundle(self):
        temporary = psycopg.connect(**database_settings())

        try:
            with self.assertRaisesRegex(
                RuntimeError,
                "injected submission failure",
            ):
                with temporary.transaction():
                    operation_id = self.operation(
                        database=temporary
                    )

                    self.audit(
                        operation_id,
                        database=temporary,
                    )

                    self.outbox(
                        operation_id,
                        database=temporary,
                    )

                    raise RuntimeError(
                        "injected submission failure"
                    )
        finally:
            temporary.rollback()
            temporary.close()

        for table in (
            "safeact_operations",
            "safeact_audit_events",
            "safeact_outbox_events",
        ):
            count = self.db.execute(
                f"""
                SELECT count(*)
                FROM public.{table}
                WHERE operation_id = %s
                """,
                (operation_id,),
            ).fetchone()[0]

            self.assertEqual(count, 0, table)

    def test_foreign_key_failure_rolls_back_prior_inserts(self):
        temporary = psycopg.connect(**database_settings())

        try:
            with self.assertRaises(
                psycopg.errors.ForeignKeyViolation
            ):
                with temporary.transaction():
                    operation_id = self.operation(
                        database=temporary
                    )

                    self.audit(
                        operation_id,
                        database=temporary,
                    )

                    self.outbox(
                        uuid4(),
                        database=temporary,
                    )
        finally:
            temporary.rollback()
            temporary.close()

        for table in (
            "safeact_operations",
            "safeact_audit_events",
        ):
            count = self.db.execute(
                f"""
                SELECT count(*)
                FROM public.{table}
                WHERE operation_id = %s
                """,
                (operation_id,),
            ).fetchone()[0]

            self.assertEqual(count, 0, table)


if __name__ == "__main__":
    unittest.main()
