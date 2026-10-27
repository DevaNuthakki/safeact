"""Alembic 0017 upgrade/downgrade round trip on a throwaway database.

History written at 0016 (never-claimed, mid-flight and finished
operations, and a cancelled one) must be backfilled exactly; the view
definition and every replaced function come back byte-identical on
downgrade; the log itself is never touched.
"""

import os
import unittest

from storage import durable_lifecycle_sql as lsql
from storage.lifecycle_sql import v0016 as OLD, v0017 as NEW
from storage.state_projection import drift_query
from tests.integration.migration_roundtrip import MigrationRoundTripCase


HISTORY = """
INSERT INTO public.safeact_operations (
    operation_id, tenant_id, request_key, action_type, resource_id,
    provider_account_id, connector_id, contract_version, payload_sha256,
    payload_canonical
)
SELECT ('00000000-0000-0000-0000-00000000000' || g)::uuid, 't', 'k' || g,
       'a', 'r', 'p', 'mock-crm', 'v1',
       encode(sha256(convert_to('{}', 'UTF8')), 'hex'), '{}'
FROM generate_series(1, 4) g;

INSERT INTO public.safeact_attempts (attempt_id, operation_id, ordinal)
VALUES ('00000000-0000-0000-0000-0000000000a2',
        '00000000-0000-0000-0000-000000000002', 1),
       ('00000000-0000-0000-0000-0000000000a3',
        '00000000-0000-0000-0000-000000000003', 1);

INSERT INTO public.safeact_operation_transitions (
    operation_id, seq, from_state, to_state, claim_id, attempt_id, actor,
    lease_expires_at, reason
) VALUES
    ('00000000-0000-0000-0000-000000000002', 1, 'RECORDED', 'CLAIMED',
     '00000000-0000-0000-0000-0000000000c2', NULL, 'w',
     now() + interval '1 hour', 'claimed'),
    ('00000000-0000-0000-0000-000000000002', 2, 'CLAIMED', 'DISPATCHING',
     '00000000-0000-0000-0000-0000000000c2',
     '00000000-0000-0000-0000-0000000000a2', 'w',
     now() + interval '1 hour', 'dispatch_risk_committed'),
    ('00000000-0000-0000-0000-000000000003', 1, 'RECORDED', 'CLAIMED',
     '00000000-0000-0000-0000-0000000000c3', NULL, 'w',
     now() + interval '1 hour', 'claimed'),
    ('00000000-0000-0000-0000-000000000003', 2, 'CLAIMED', 'DISPATCHING',
     '00000000-0000-0000-0000-0000000000c3',
     '00000000-0000-0000-0000-0000000000a3', 'w',
     now() + interval '1 hour', 'dispatch_risk_committed'),
    ('00000000-0000-0000-0000-000000000003', 3, 'DISPATCHING', 'UNKNOWN',
     '00000000-0000-0000-0000-0000000000c3',
     '00000000-0000-0000-0000-0000000000a3', 'w', NULL,
     'ambiguous_no_response'),
    ('00000000-0000-0000-0000-000000000004', 1, 'RECORDED',
     'CANCELLED_BEFORE_CLAIM', gen_random_uuid(), NULL, 'a', NULL,
     'cancelled_before_claim')
"""


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class Migration0017RoundTripTests(MigrationRoundTripCase):

    def view(self):
        return self.query(
            "SELECT pg_get_viewdef('public.safeact_operation_current_state')"
        )

    def log(self):
        return self.query("""
            SELECT operation_id, seq, to_state, claim_id, attempt_id,
                   actor, lease_expires_at, reason, created_at
            FROM public.safeact_operation_transitions
            ORDER BY operation_id, seq
        """)

    def drift(self):
        return self.query(drift_query("public").as_string(None))

    def test_upgrade_backfills_and_downgrade_restores(self):
        self.migrate(self.upgrade, OLD.REVISION)
        self.execute(HISTORY)
        before, view, log = self.functions(), self.view(), self.log()
        states = self.query("""
            SELECT operation_id, seq, state
            FROM public.safeact_operation_current_state ORDER BY 1
        """)

        self.migrate(self.upgrade, NEW.REVISION)
        upgraded = self.functions()
        self.assertEqual(set(upgraded) - set(before), set(NEW.NEW_FUNCTIONS))
        for name in NEW.REPLACED_FUNCTIONS:
            self.assertNotEqual(upgraded[name][0], before[name][0])
            self.assertEqual(upgraded[name][1:], before[name][1:])
        self.assertNotEqual(self.view(), view)

        self.assertEqual(self.drift(), [])
        self.assertEqual(self.query(
            "SELECT operation_id, seq, state "
            "FROM public.safeact_state_projection ORDER BY 1"
        ), states)
        self.assertEqual(self.query("""
            SELECT operation_id, seq, state
            FROM public.safeact_operation_current_state ORDER BY 1
        """), states)

        self.migrate(self.downgrade, OLD.REVISION)
        self.assertEqual(self.functions(), before)
        self.assertEqual(self.view(), view)
        self.assertEqual(self.log(), log)
        self.assertNotIn("safeact_state_projection", self.relations())

        self.migrate(self.upgrade, NEW.REVISION)
        self.assertEqual(self.functions(), upgraded)
        self.assertEqual(self.drift(), [])

    def test_new_operations_get_a_row_after_upgrade(self):
        self.migrate(self.upgrade, NEW.REVISION)
        self.execute(HISTORY.split(";")[0])
        self.assertEqual(self.query(
            "SELECT count(*), min(seq), max(seq) "
            "FROM public.safeact_state_projection"
        ), [(4, 0, 0)])
        self.assertEqual(self.drift(), [])

    def test_autogenerate_never_proposes_dropping_head_objects(self):
        self.migrate(self.upgrade, "head")
        self.assertEqual(
            self.removals(lsql.autogenerate_include_object), set()
        )


if __name__ == "__main__":
    unittest.main()
