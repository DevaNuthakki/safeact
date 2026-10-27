"""Disposable-schema atomic admission security tests.

These tests never apply migrations, create persistent
authorization data, or enable provider dispatch.
"""

import os
import unittest
from hashlib import sha256
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.errors import (
    CheckViolation,
    InsufficientPrivilege,
    LockNotAvailable,
)
import sqlalchemy as sa

from storage.atomic_gate_sql import (
    create_gate_sql,
    gate_signature_sql,
    gate_call_sql,
)
from storage.authority_schema import authority_metadata
from storage.schema import metadata as bundle_metadata
from storage.database import local_database_url
from tests.integration.test_postgres_smoke import database_settings


BUNDLES = (
    "safeact_operations",
    "safeact_attempts",
    "safeact_audit_events",
    "safeact_outbox_events",
)

AUTH = (
    "safeact_auth_actors",
    "safeact_auth_identities",
    "safeact_auth_resources",
    "safeact_auth_grants",
)

PAYLOAD = '{"case_id":"resource-a"}'
DIGEST = sha256(PAYLOAD.encode()).hexdigest()


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1"
    and os.environ.get("SAFEACT_RUN_DB_SECURITY_TESTS") == "1",
    "Disposable database security tests are disabled",
)
class AtomicGateSecurityTests(unittest.TestCase):

    def setUp(self):
        suffix = uuid4().hex
        self.schema = "safeact_it_" + suffix
        self.owner_name = "safeact_fn_it_" + suffix
        self.runtime_name = "safeact_runtime_it_" + suffix

        self.S = sql.Identifier(self.schema)
        self.OWNER = sql.Identifier(self.owner_name)
        self.RUNTIME = sql.Identifier(self.runtime_name)

        self.admin = None
        self.engine = None
        self.client = None

        self.addCleanup(self._cleanup)

        self.admin = psycopg.connect(
            **database_settings(),
            autocommit=True,
        )

        self.admin.execute(
            sql.SQL("CREATE SCHEMA {}").format(self.S)
        )

        self.admin.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN").format(self.OWNER)
        )

        self.admin.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN").format(self.RUNTIME)
        )

        self.engine = sa.create_engine(
            local_database_url(),
            pool_pre_ping=True,
        )

        with self.engine.begin() as connection:
            scoped = connection.execution_options(
                schema_translate_map={None: self.schema}
            )

            bundle_metadata.create_all(scoped)
            authority_metadata.create_all(scoped)

        self.admin.execute(sql.SQL("""
            INSERT INTO {}.safeact_auth_actors(
                tenant_id,actor_id,enabled
            ) VALUES
              ('tenant-a','actor-a',TRUE),
              ('tenant-a','actor-b',TRUE)
        """).format(self.S))

        self.admin.execute(sql.SQL("""
            INSERT INTO {}.safeact_auth_identities(
                issuer,subject,tenant_id,actor_id,enabled
            ) VALUES
              (
                'https://id.example','subject-a',
                'tenant-a','actor-a',TRUE
              ),
              (
                'https://id.example','subject-b',
                'tenant-a','actor-b',TRUE
              )
        """).format(self.S))

        self.admin.execute(sql.SQL("""
            INSERT INTO {}.safeact_auth_resources(
                tenant_id,resource_id,connector_id,
                provider_account_id,enabled
            ) VALUES(
                'tenant-a','resource-a',
                'connector-a','account-a',TRUE
            )
        """).format(self.S))

        self.admin.execute(sql.SQL("""
            INSERT INTO {}.safeact_auth_grants(
                tenant_id,actor_id,resource_id,connector_id,
                provider_account_id,action_type,
                contract_version,enabled
            ) VALUES
              (
                'tenant-a','actor-a','resource-a',
                'connector-a','account-a',
                'create_case','v1',TRUE
              ),
              (
                'tenant-a','actor-b','resource-a',
                'connector-a','account-a',
                'create_case','v1',TRUE
              )
        """).format(self.S))

        self.admin.execute(sql.SQL(
            "GRANT USAGE ON SCHEMA {} TO {}, {}"
        ).format(self.S, self.OWNER, self.RUNTIME))

        for table in AUTH:
            self.admin.execute(sql.SQL(
                "GRANT SELECT, UPDATE ON {}.{} TO {}"
            ).format(
                self.S, sql.Identifier(table), self.OWNER
            ))

        for table in (
            "safeact_operations",
            "safeact_audit_events",
            "safeact_outbox_events",
        ):
            self.admin.execute(sql.SQL(
                "GRANT SELECT, INSERT ON {}.{} TO {}"
            ).format(
                self.S, sql.Identifier(table), self.OWNER
            ))

        self.admin.execute(sql.SQL(
            "GRANT USAGE, SELECT "
            "ON ALL SEQUENCES IN SCHEMA {} TO {}"
        ).format(self.S, self.OWNER))

        self.admin.execute(create_gate_sql(self.schema))

        signature = gate_signature_sql(self.schema)

        self.admin.execute(sql.SQL(
            "ALTER FUNCTION {} OWNER TO {}"
        ).format(signature, self.OWNER))

        self.admin.execute(sql.SQL(
            "REVOKE ALL ON FUNCTION {} FROM PUBLIC"
        ).format(signature))

        self.admin.execute(sql.SQL(
            "GRANT EXECUTE ON FUNCTION {} TO {}"
        ).format(signature, self.RUNTIME))

        self.client = psycopg.connect(**database_settings())

    def _cleanup(self):
        if self.client is not None:
            try:
                self.client.rollback()
            finally:
                self.client.close()

        if self.engine is not None:
            self.engine.dispose()

        if self.admin is not None:
            try:
                self.admin.execute(
                    "SET lock_timeout = '5000ms'"
                )

                self.admin.execute(sql.SQL(
                    "DROP SCHEMA IF EXISTS {} CASCADE"
                ).format(self.S))

                for role in (self.OWNER, self.RUNTIME):
                    self.admin.execute(sql.SQL(
                        "DROP ROLE IF EXISTS {}"
                    ).format(role))
            finally:
                self.admin.close()

    def args(
        self,
        *,
        subject="subject-a",
        key="req-001",
        resource="resource-a",
        action="create_case",
        payload=PAYLOAD,
        digest=DIGEST,
    ):
        return (
            "https://id.example",
            subject,
            key,
            action,
            resource,
            "connector-a",
            "account-a",
            "v1",
            payload,
            digest,
            None,
        )

    def call(self, **kwargs):
        self.client.execute(
            sql.SQL("SET LOCAL ROLE {}").format(self.RUNTIME)
        )

        return self.client.execute(
            gate_call_sql(self.schema),
            self.args(**kwargs),
        ).fetchone()

    def counts(self):
        return tuple(
            self.admin.execute(sql.SQL(
                "SELECT count(*) FROM {}.{}"
            ).format(
                self.S, sql.Identifier(table)
            )).fetchone()[0]
            for table in BUNDLES
        )

    def test_atomic_commit_and_same_actor_replay(self):
        first = self.call()

        self.assertTrue(first[1])
        self.assertEqual(self.counts(), (0, 0, 0, 0))

        self.client.commit()

        self.assertEqual(self.counts(), (1, 0, 1, 1))

        again = self.call()

        self.assertEqual(again, (first[0], False))
        self.client.commit()

        self.assertEqual(self.counts(), (1, 0, 1, 1))

    def test_cross_actor_request_key_and_payload_conflicts(self):
        self.call()
        self.client.commit()

        with self.assertRaises(InsufficientPrivilege):
            self.call(subject="subject-b")

        self.client.rollback()

        different = '{"case_id":"other"}'

        with self.assertRaises(psycopg.errors.RaiseException):
            self.call(
                payload=different,
                digest=sha256(
                    different.encode()
                ).hexdigest(),
            )

        self.client.rollback()
        self.assertEqual(self.counts(), (1, 0, 1, 1))

    def test_unknown_scope_and_disabled_resource_deny(self):
        with self.assertRaises(InsufficientPrivilege):
            self.call(resource="not-granted")

        self.client.rollback()

        self.admin.execute(sql.SQL(
            "UPDATE {}.safeact_auth_resources "
            "SET enabled=FALSE"
        ).format(self.S))

        with self.assertRaises(InsufficientPrivilege):
            self.call()

        self.client.rollback()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_revocation_blocks_until_admission_commits(self):
        self.call()

        revoker = psycopg.connect(**database_settings())

        try:
            revoker.execute(
                "SET LOCAL lock_timeout='300ms'"
            )

            with self.assertRaises(LockNotAvailable):
                revoker.execute(sql.SQL(
                    "UPDATE {}.safeact_auth_grants "
                    "SET enabled=FALSE "
                    "WHERE actor_id='actor-a'"
                ).format(self.S))

            revoker.rollback()
            self.client.commit()

            revoker.execute(sql.SQL(
                "UPDATE {}.safeact_auth_grants "
                "SET enabled=FALSE "
                "WHERE actor_id='actor-a'"
            ).format(self.S))

            revoker.commit()

            with self.assertRaises(InsufficientPrivilege):
                self.call(key="req-after-revocation")

            self.client.rollback()
            self.assertEqual(self.counts(), (1, 0, 1, 1))
        finally:
            revoker.rollback()
            revoker.close()

    def test_runtime_cannot_access_tables_directly(self):
        for table in AUTH + BUNDLES:
            qualified = f"{self.schema}.{table}"

            privileges = self.admin.execute("""
                SELECT
                    has_table_privilege(%s,%s,'SELECT'),
                    has_table_privilege(%s,%s,'INSERT'),
                    has_table_privilege(%s,%s,'UPDATE'),
                    has_table_privilege(%s,%s,'DELETE')
            """, (
                self.runtime_name, qualified,
                self.runtime_name, qualified,
                self.runtime_name, qualified,
                self.runtime_name, qualified,
            )).fetchone()

            self.assertEqual(
                privileges,
                (False, False, False, False),
            )

        self.client.execute(
            sql.SQL("SET LOCAL ROLE {}").format(self.RUNTIME)
        )

        with self.assertRaises(InsufficientPrivilege):
            self.client.execute(sql.SQL(
                "SELECT * FROM {}.safeact_operations"
            ).format(self.S))

        self.client.rollback()

    def test_rollback_and_digest_check(self):
        self.assertTrue(self.call()[1])
        self.client.rollback()

        self.assertEqual(self.counts(), (0, 0, 0, 0))

        with self.assertRaises(CheckViolation):
            self.call(
                key="req-invalid-digest",
                digest="0" * 64,
            )

        self.client.rollback()
        self.assertEqual(self.counts(), (0, 0, 0, 0))



    def test_disabled_actor_denies_admission(self):
        self.admin.execute(sql.SQL(
            "UPDATE {}.safeact_auth_actors "
            "SET enabled=FALSE WHERE actor_id='actor-a'"
        ).format(self.S))

        with self.assertRaises(InsufficientPrivilege):
            self.call(key="disabled-actor")

        self.client.rollback()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_disabled_identity_denies_admission(self):
        self.admin.execute(sql.SQL(
            "UPDATE {}.safeact_auth_identities "
            "SET enabled=FALSE WHERE subject='subject-a'"
        ).format(self.S))

        with self.assertRaises(InsufficientPrivilege):
            self.call(key="disabled-identity")

        self.client.rollback()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_disabled_grant_denies_admission(self):
        self.admin.execute(sql.SQL(
            "UPDATE {}.safeact_auth_grants "
            "SET enabled=FALSE WHERE actor_id='actor-a'"
        ).format(self.S))

        with self.assertRaises(InsufficientPrivilege):
            self.call(key="disabled-grant")

        self.client.rollback()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_missing_original_outbox_denies_replay(self):
        first = self.call()
        self.assertTrue(first[1])
        self.client.commit()

        self.admin.execute(
            sql.SQL(
                "DELETE FROM {}.safeact_outbox_events "
                "WHERE operation_id=%s"
            ).format(self.S),
            (first[0],),
        )

        with self.assertRaises(psycopg.errors.RaiseException):
            self.call()

        self.client.rollback()

        self.assertEqual(self.counts(), (1, 0, 1, 0))

    def test_legacy_writer_cannot_execute_prototype_gate(self):
        signature = (
            self.schema
            + ".admit_real_intent("
            + ",".join(("text",) * 11)
            + ")"
        )

        permitted = self.admin.execute(
            """
            SELECT has_function_privilege(
                %s, %s, 'EXECUTE'
            )
            """,
            (
                "safeact_admission_writer",
                signature,
            ),
        ).fetchone()[0]

        self.assertIs(permitted, False)

    def test_negative_control_runtime_can_assert_other_subject(self):
        # Deliberately demonstrates an OPEN vulnerability.
        # This must not be interpreted as secure identity proof.
        first = self.call(key="claim-a")
        self.assertTrue(first[1])
        self.client.commit()

        second = self.call(
            subject="subject-b",
            key="claim-b",
        )

        self.assertTrue(second[1])
        self.client.commit()

        actor = self.admin.execute(
            sql.SQL(
                "SELECT actor_id FROM {}.safeact_audit_events "
                "WHERE operation_id=%s "
                "AND event_type='INTENT_RECORDED'"
            ).format(self.S),
            (second[0],),
        ).fetchone()[0]

        self.assertEqual(actor, "actor-b")

        print(
            "KNOWN LIMITATION: One runtime credential "
            "can assert another enabled subject."
        )

    def test_negative_control_noncanonical_json_is_accepted(self):
        # The database validates JSON and SHA-256 binding,
        # but it does not enforce application canonicalization.
        noncanonical = '{ "case_id" : "resource-a" }'

        calculated = sha256(
            noncanonical.encode("utf-8")
        ).hexdigest()

        result = self.call(
            key="noncanonical-probe",
            payload=noncanonical,
            digest=calculated,
        )

        self.assertTrue(result[1])
        self.client.commit()

        stored = self.admin.execute(
            sql.SQL(
                "SELECT payload_canonical "
                "FROM {}.safeact_operations "
                "WHERE operation_id=%s"
            ).format(self.S),
            (result[0],),
        ).fetchone()[0]

        self.assertEqual(stored, noncanonical)

        print(
            "KNOWN LIMITATION: SQL gate does not "
            "enforce canonical JSON encoding."
        )

if __name__ == "__main__":
    unittest.main()
