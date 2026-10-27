"""Registered connector contracts and the fence's contract requirement.

Disposable schemas only, except the last test, which checks (read-only)
that the dev database's public schema registers no contract at all.
"""

import os
import unittest

from dataclasses import replace
from datetime import timedelta

import psycopg
from psycopg import sql

from core.connector_profiles import (
    MOCK_PROFILES,
    PROFILE_COLUMNS,
    payments_profile,
)
from core.lifecycle import LifecycleState as S
from storage import durable_lifecycle as L
from storage import durable_lifecycle_sql as lsql
from tests.integration.lifecycle_fixture import (
    DisposableLifecycleDatabase,
    commit,
)
from tests.integration.test_postgres_smoke import database_settings


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class ConnectorContractTests(unittest.TestCase):

    def setUp(self):
        self.db = DisposableLifecycleDatabase()
        self.addCleanup(self.db.cleanup)
        self.db.start()
        self.schema = self.db.schema

    def insert(self, connection, row):
        connection.execute(
            sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                self.db.table("safeact_connector_contracts"),
                sql.SQL(", ").join(map(sql.Identifier, PROFILE_COLUMNS)),
                sql.SQL(", ").join(
                    sql.Placeholder() for _ in PROFILE_COLUMNS
                ),
            ),
            tuple(row[c] for c in PROFILE_COLUMNS),
        )

    def test_registered_profiles_read_back_exactly(self):
        for role in (self.db.worker_a, self.db.recoverer):
            connection = self.db.connect(role)
            for profile in MOCK_PROFILES:
                with self.subTest(role=role, profile=profile.version):
                    self.assertEqual(
                        commit(connection, lambda: L.connector_contract(
                            connection, self.schema,
                            profile.connector_id, profile.version,
                        )),
                        profile,
                    )

    def test_unknown_contract_is_refused(self):
        connection = self.db.connect(self.db.recoverer)
        with self.assertRaises(L.ContractUnknown):
            commit(connection, lambda: L.connector_contract(
                connection, self.schema, "mock-payments", "v9",
            ))

    def test_fence_refuses_operation_without_registered_contract(self):
        operation_id = self.db.admit(
            request_key="unregistered", contract_version="v9"
        )
        worker = self.db.connect(self.db.worker_a)
        held = commit(worker, lambda: L.claim(
            worker, self.schema, operation_id, lease_seconds=60,
        ))

        with self.assertRaises(L.ContractUnknown):
            commit(worker, lambda: L.begin_dispatch(
                worker, self.schema, held, lease_seconds=60,
            ))

        self.assertEqual(
            self.db.count("safeact_attempts", operation_id=operation_id), 0
        )
        state = L.current_state(worker, self.schema, operation_id)
        self.assertEqual((state.state, state.seq), (S.CLAIMED, 1))

    def test_contracts_are_immutable(self):
        for statement in (
            "UPDATE {} SET max_attempts = 9",
            "DELETE FROM {}",
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(psycopg.errors.CheckViolation):
                    self.db.admin.execute(sql.SQL(statement).format(
                        self.db.table("safeact_connector_contracts")
                    ))

    def test_margin_must_cover_request_deadline(self):
        row = payments_profile("v-margin").row()
        row.update(
            key_safety_margin_seconds=29, request_deadline_seconds=30
        )
        with self.assertRaisesRegex(
            psycopg.errors.CheckViolation, "deadline_margin"
        ):
            with self.db.admin.transaction():
                self.insert(self.db.admin, row)

        row.update(key_safety_margin_seconds=30)
        with self.db.admin.transaction():
            self.insert(self.db.admin, row)

    def test_native_key_fields_must_be_all_or_nothing(self):
        row = payments_profile("v-native").row()
        row.update(native_idempotency=False)
        with self.assertRaisesRegex(
            psycopg.errors.CheckViolation, "native"
        ):
            with self.db.admin.transaction():
                self.insert(self.db.admin, row)

    def test_profile_refuses_margin_below_deadline_in_python_too(self):
        with self.assertRaisesRegex(ValueError, "request deadline"):
            replace(
                payments_profile("v-py"),
                request_deadline=timedelta(hours=2),
            )

    def test_callers_cannot_write_or_read_contracts_directly(self):
        row = payments_profile("v-caller").row()
        for role in (self.db.worker_a, self.db.recoverer):
            connection = self.db.connect(role)
            with self.subTest(role=role):
                with self.assertRaises(
                    psycopg.errors.InsufficientPrivilege
                ):
                    self.insert(connection, row)
                connection.rollback()
                with self.assertRaises(
                    psycopg.errors.InsufficientPrivilege
                ):
                    connection.execute(sql.SQL("SELECT * FROM {}").format(
                        self.db.table("safeact_connector_contracts")
                    ))
                connection.rollback()

        stranger = self.db.connect(self.db.stranger)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            L.connector_contract(
                stranger, self.schema, "mock-payments", "v1"
            )


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1",
    "Explicit PostgreSQL opt-in required",
)
class PublicSchemaContractTests(unittest.TestCase):

    def test_public_schema_registers_no_contract(self):
        with psycopg.connect(**database_settings()) as db:
            db.execute("SET TRANSACTION READ ONLY")
            self.assertEqual(
                db.execute(
                    "SELECT version_num FROM public.alembic_version"
                ).fetchone()[0],
                lsql.HEAD_REVISION,
            )
            self.assertEqual(db.execute(
                "SELECT count(*) FROM public.safeact_connector_contracts"
            ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
