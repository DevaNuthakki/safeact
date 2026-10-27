"""Alembic 0005 upgrade/downgrade round trip on a throwaway database."""

import os
import unittest

from uuid import uuid4

from storage import durable_lifecycle_sql as lsql
from storage.lifecycle_sql import v0004, v0005
from tests.integration.migration_roundtrip import MigrationRoundTripCase


CHAIN = (
    ("RECORDED", "CLAIMED"),
    ("CLAIMED", "DISPATCHING"),
    ("DISPATCHING", "UNKNOWN"),
    ("UNKNOWN", "VERIFYING"),
    ("VERIFYING", "ESCALATED_UNRESOLVED"),
    ("ESCALATED_UNRESOLVED", "VERIFYING"),
)


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class Migration0005RoundTripTests(MigrationRoundTripCase):

    def legal_pairs(self):
        return set(self.query(
            "SELECT from_state, to_state FROM public.safeact_legal_transitions"
        ))

    def test_upgrade_downgrade_upgrade_restores_earlier_functions(self):
        self.migrate(self.upgrade, v0004.REVISION)
        before = self.functions()
        pairs_before = self.legal_pairs()

        self.migrate(self.upgrade, v0005.REVISION)
        upgraded = self.functions()
        self.assertEqual(
            set(upgraded) - set(before), set(v0005.NEW_FUNCTIONS)
        )
        self.assertEqual(
            self.legal_pairs() - pairs_before, set(v0005.LEGAL_TRANSITIONS)
        )
        for name in v0005.REPLACED_FUNCTIONS:
            with self.subTest(replaced=name):
                self.assertNotEqual(upgraded[name][0], before[name][0])
                self.assertEqual(upgraded[name][1:], before[name][1:])
        for name in v0005.NEW_FUNCTIONS:
            owner, acl = upgraded[name][1:]
            self.assertEqual(
                (acl or "{}").strip("{}").split(","), [f"{owner}=X/{owner}"]
            )

        self.migrate(self.downgrade, v0004.REVISION)
        self.assertEqual(self.functions(), before)
        self.assertEqual(self.legal_pairs(), pairs_before)
        # The write guard is back on the legal-pairs table.
        self.assertEqual(self.query("""
            SELECT tgenabled FROM pg_trigger
            WHERE tgname = 'safeact_legal_transitions_write_guard'
        """), [("O",)])

        self.migrate(self.upgrade, v0005.REVISION)
        self.assertEqual(self.functions(), upgraded)

    def test_downgrade_refuses_after_a_reopen(self):
        self.migrate(self.upgrade, v0005.REVISION)
        operation_id, attempt_id = uuid4(), uuid4()
        self.execute("""
            INSERT INTO public.safeact_operations (
                operation_id, tenant_id, request_key, action_type,
                resource_id, provider_account_id, connector_id,
                contract_version, payload_sha256, payload_canonical
            ) VALUES (
                :id, 't', 'k', 'a', 'r', 'p', 'c', 'v1',
                encode(sha256(convert_to('{}', 'UTF8')), 'hex'), '{}'
            )
        """, {"id": operation_id})
        self.execute("""
            INSERT INTO public.safeact_attempts (attempt_id, operation_id, ordinal)
            VALUES (:a, :id, 1)
        """, {"a": attempt_id, "id": operation_id})
        for seq, (a, b) in enumerate(CHAIN, start=1):
            self.execute("""
                INSERT INTO public.safeact_operation_transitions (
                    operation_id, seq, from_state, to_state, claim_id,
                    attempt_id, actor, lease_expires_at, reason
                ) VALUES (
                    :id, :seq, :a, :b, gen_random_uuid(), :attempt,
                    'test', now(), 'history'
                )
            """, {"id": operation_id, "seq": seq, "a": a, "b": b,
                  "attempt": attempt_id})

        with self.assertRaisesRegex(RuntimeError, "execution history"):
            self.migrate(self.downgrade, v0004.REVISION)
        self.assertEqual(self.version(), [(v0005.REVISION,)])

    def test_autogenerate_never_proposes_dropping_head_objects(self):
        self.migrate(self.upgrade, "head")
        self.assertEqual(
            self.removals(lsql.autogenerate_include_object), set()
        )


if __name__ == "__main__":
    unittest.main()
