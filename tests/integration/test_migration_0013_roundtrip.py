"""Alembic 0013 upgrade/downgrade round trip on a throwaway database."""

import os
import unittest

from storage import durable_lifecycle_sql as lsql
from storage.lifecycle_sql import v0012 as OLD, v0013 as NEW
from tests.integration.migration_roundtrip import MigrationRoundTripCase


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class Migration0013RoundTripTests(MigrationRoundTripCase):

    def columns(self):
        return [row[0] for row in self.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "AND table_name = 'safeact_attempt_outcomes' ORDER BY 1"
        )]

    def test_upgrade_downgrade_upgrade(self):
        self.migrate(self.upgrade, OLD.REVISION)
        before, before_columns = self.functions(), self.columns()

        self.migrate(self.upgrade, NEW.REVISION)
        upgraded = self.functions()
        self.assertEqual(set(upgraded) - set(before), set(NEW.NEW_FUNCTIONS))
        for name in NEW.REPLACED_FUNCTIONS:
            self.assertNotEqual(upgraded[name][0], before[name][0])
            self.assertEqual(upgraded[name][1:], before[name][1:])
        self.assertEqual(
            set(self.columns()) - set(before_columns), {"provenance"}
        )

        self.migrate(self.downgrade, OLD.REVISION)
        self.assertEqual(self.functions(), before)
        self.assertEqual(self.columns(), before_columns)

        self.migrate(self.upgrade, NEW.REVISION)
        self.assertEqual(self.functions(), upgraded)

    def test_downgrade_refuses_while_review_history_exists(self):
        self.migrate(self.upgrade, NEW.REVISION)
        self.execute("""
            INSERT INTO public.safeact_operations (
                operation_id, tenant_id, request_key, action_type,
                resource_id, provider_account_id, connector_id,
                contract_version, payload_sha256, payload_canonical
            ) VALUES (
                '00000000-0000-0000-0000-000000000001', 't', 'k', 'a', 'r',
                'p', 'mock-crm', 'v1',
                encode(sha256(convert_to('{}', 'UTF8')), 'hex'), '{}'
            );
            INSERT INTO public.safeact_principals (
                principal_id, tenant_id, username, kind, display_name, demo
            ) VALUES (
                '00000000-0000-0000-0000-000000000002', 't', 'ops-person',
                'human', 'Ops', false
            );
            INSERT INTO public.safeact_proposals (
                proposal_id, operation_id, tenant_id, action, rationale,
                proposed_by, bound_seq, bound_state, bound_payload_sha256,
                bound_evidence_digest, bound_connector_id,
                bound_contract_version, binding_digest, expires_at
            ) VALUES (
                gen_random_uuid(), '00000000-0000-0000-0000-000000000001',
                't', 'REVERIFY', 'x', '00000000-0000-0000-0000-000000000002',
                0, 'RECORDED', repeat('0', 64), repeat('0', 64), 'mock-crm',
                'v1', repeat('0', 64), now() + interval '1 hour'
            )
        """)

        with self.assertRaisesRegex(RuntimeError, "Review history exists"):
            self.migrate(self.downgrade, OLD.REVISION)
        self.assertEqual(self.version(), [(NEW.REVISION,)])

    def test_autogenerate_never_proposes_dropping_head_objects(self):
        self.migrate(self.upgrade, "head")
        self.assertEqual(
            self.removals(lsql.autogenerate_include_object), set()
        )


if __name__ == "__main__":
    unittest.main()
