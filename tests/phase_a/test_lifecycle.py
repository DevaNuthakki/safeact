"""Durable lifecycle model and SQL builder regression tests.

No database is contacted: SQL is rendered without a connection.
"""

import re
import unittest

from pathlib import Path

from core.lifecycle import (
    AMBIGUOUS_REASONS,
    RETRY_PENDING_REASON,
    CANCEL_TRANSITIONS,
    COMPLETED_STATES,
    DISPATCH_OUTCOMES,
    LEGAL_TRANSITIONS,
    RECLAIM_TRANSITIONS,
    RECOVERY_TRANSITIONS,
    RELEASE_TRANSITIONS,
    UNRESOLVED_STATES,
    LifecycleState as S,
    is_legal,
)
from storage import durable_lifecycle_sql as lsql


SCHEMA = "safeact_it_" + "0" * 32
DEFINITIONS = ("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION")


def render(statements):
    return [statement.as_string() for statement in statements]


class LifecycleModelTests(unittest.TestCase):

    def test_section_05_transitions_are_legal(self):
        for pair in (
            (S.RECORDED, S.CLAIMED),
            (S.CLAIMED, S.DISPATCHING),
            (S.DISPATCHING, S.SUCCEEDED),
            (S.DISPATCHING, S.FAILED_CONFIRMED),
            (S.DISPATCHING, S.UNKNOWN),
            (S.UNKNOWN, S.VERIFYING),
            (S.VERIFYING, S.SUCCEEDED),
            (S.VERIFYING, S.RETRY_ELIGIBLE),
            (S.VERIFYING, S.UNKNOWN),
            (S.VERIFYING, S.ESCALATED_UNRESOLVED),
            (S.RETRY_ELIGIBLE, S.CLAIMED),
        ):
            with self.subTest(pair=pair):
                self.assertIn(pair, LEGAL_TRANSITIONS)

    def test_skips_and_reversals_are_illegal(self):
        for pair in (
            (S.RECORDED, S.DISPATCHING),
            (S.RECORDED, S.SUCCEEDED),
            (S.CLAIMED, S.SUCCEEDED),
            (S.CLAIMED, S.RECORDED),
            (S.DISPATCHING, S.CLAIMED),
            (S.DISPATCHING, S.RETRY_ELIGIBLE),
            (S.UNKNOWN, S.SUCCEEDED),
            (S.UNKNOWN, S.FAILED_CONFIRMED),
            (S.UNKNOWN, S.RETRY_ELIGIBLE),
            (S.RETRY_ELIGIBLE, S.DISPATCHING),
        ):
            with self.subTest(pair=pair):
                self.assertFalse(is_legal(*pair))

    def test_terminal_states_have_no_exits(self):
        for state in COMPLETED_STATES:
            with self.subTest(state=state):
                self.assertFalse(
                    any(a is state for a, _ in LEGAL_TRANSITIONS)
                )

    def test_escalation_exits_only_by_reopening_verification(self):
        for state in UNRESOLVED_STATES:
            self.assertEqual(
                {b for a, b in LEGAL_TRANSITIONS if a is state},
                {S.VERIFYING},
            )
            self.assertIn((state, S.VERIFYING), RECOVERY_TRANSITIONS)

    def test_nothing_returns_to_recorded(self):
        self.assertFalse(
            any(b is S.RECORDED for _, b in LEGAL_TRANSITIONS)
        )

    def test_every_state_is_reachable(self):
        reached = {S.RECORDED}
        changed = True
        while changed:
            changed = False
            for a, b in LEGAL_TRANSITIONS:
                if a in reached and b not in reached:
                    reached.add(b)
                    changed = True
        self.assertEqual(reached, set(S))

    def test_unresolved_is_never_completed(self):
        self.assertFalse(COMPLETED_STATES & UNRESOLVED_STATES)

    def test_function_families_cover_every_legal_pair(self):
        reachable = (
            {(S.RECORDED, S.CLAIMED), (S.CLAIMED, S.DISPATCHING)}
            | {(S.DISPATCHING, b) for b in DISPATCH_OUTCOMES}
            | RECOVERY_TRANSITIONS
            | RELEASE_TRANSITIONS
            | RECLAIM_TRANSITIONS
            | CANCEL_TRANSITIONS
        )
        self.assertEqual(reachable, LEGAL_TRANSITIONS)
        self.assertFalse(CANCEL_TRANSITIONS & RECOVERY_TRANSITIONS)
        self.assertTrue(RECOVERY_TRANSITIONS <= LEGAL_TRANSITIONS)

    def test_reclaim_is_only_through_the_claim_function(self):
        self.assertEqual(
            RECLAIM_TRANSITIONS, {(S.RETRY_ELIGIBLE, S.CLAIMED)}
        )
        self.assertFalse(RECLAIM_TRANSITIONS & RECOVERY_TRANSITIONS)

    def test_unknown_reason_vocabulary_matches_sql(self):
        self.assertEqual(AMBIGUOUS_REASONS, set(lsql.AMBIGUOUS_REASONS))
        self.assertEqual(RETRY_PENDING_REASON, lsql.RETRY_PENDING_REASON)
        self.assertNotIn(RETRY_PENDING_REASON, AMBIGUOUS_REASONS)

    def test_claimed_exits_never_dispatch_or_complete(self):
        exits = {b for a, b in LEGAL_TRANSITIONS if a is S.CLAIMED}
        self.assertEqual(
            exits, {S.DISPATCHING, S.RETRY_ELIGIBLE, S.UNKNOWN}
        )
        self.assertEqual(
            RELEASE_TRANSITIONS,
            {(S.CLAIMED, S.RETRY_ELIGIBLE), (S.CLAIMED, S.UNKNOWN)},
        )
        self.assertTrue(RELEASE_TRANSITIONS <= RECOVERY_TRANSITIONS)

    def test_unknown_state_names_raise(self):
        with self.assertRaises(ValueError):
            is_legal("RECORDED", "DONE")


class LifecycleSqlBuilderTests(unittest.TestCase):

    def test_schema_must_be_public_or_disposable(self):
        for schema in (
            "other",
            "safeact_it_" + "0" * 31,
            'safeact_it_' + "0" * 32 + '"; DROP',
            None,
        ):
            with self.subTest(schema=schema):
                with self.assertRaises(ValueError):
                    lsql.upgrade_statements(schema)

        self.assertTrue(lsql.upgrade_statements("public"))

    def test_grants_refuse_public_schema_and_bad_roles(self):
        with self.assertRaises(ValueError):
            lsql.disposable_grant_statements(
                "public", owner="safeact_o",
                workers=(), recoverers=(),
            )

        with self.assertRaises(ValueError):
            lsql.disposable_grant_statements(
                SCHEMA, owner="postgres",
                workers=(), recoverers=(),
            )

    def test_every_function_pins_search_path(self):
        functions = [
            text for text in render(lsql.upgrade_statements(SCHEMA))
            if text.startswith("CREATE FUNCTION")
        ]
        self.assertEqual(len(functions), len(lsql.ALL_FUNCTIONS))

        replaced = [
            text for text in render(lsql.upgrade_statements(SCHEMA))
            if text.startswith("CREATE OR REPLACE FUNCTION")
        ]
        for text in functions + replaced:
            with self.subTest(function=text[:60]):
                self.assertIn(
                    "SET search_path = pg_catalog, pg_temp", text
                )

    def test_replaced_functions_stay_protected(self):
        # A later migration's CREATE OR REPLACE must keep the hardening.
        for text in render(lsql.upgrade_statements(SCHEMA)):
            if text.startswith("CREATE OR REPLACE FUNCTION"):
                name = re.match(r'[^"]*"[^"]+"\."([^"]+)"', text).group(1)
                with self.subTest(function=name):
                    self.assertIn(name, lsql.ALL_FUNCTIONS)
                    self.assertEqual(
                        "SECURITY DEFINER" in text,
                        name in lsql.DEFINER_FUNCTIONS,
                    )

    def test_protected_functions_are_security_definer(self):
        texts = render(lsql.upgrade_statements(SCHEMA))

        for name in lsql.ALL_FUNCTIONS:
            text = next(
                t for t in texts
                if t.startswith(f'CREATE FUNCTION "{SCHEMA}"."{name}"(')
            )
            with self.subTest(name=name):
                self.assertEqual(
                    "SECURITY DEFINER" in text,
                    name in lsql.DEFINER_FUNCTIONS,
                )

    def test_execute_revoked_from_public_for_every_function(self):
        texts = render(lsql.upgrade_statements(SCHEMA))

        for name in lsql.ALL_FUNCTIONS:
            with self.subTest(name=name):
                self.assertIn(
                    f'REVOKE ALL ON FUNCTION "{SCHEMA}"."{name}"('
                    f"{lsql.ALL_FUNCTIONS[name]}) FROM PUBLIC",
                    texts,
                )

    def test_function_bodies_qualify_lifecycle_objects(self):
        unqualified = re.compile(
            r"(?<![\".])\bsafeact_(operations|attempts|audit_events|"
            r"outbox_events|operation_transitions|legal_transitions|"
            r"attempt_outcomes|outbox_deliveries|connector_contracts)"
            r"\b(?!\")"
        )
        for text in render(lsql.upgrade_statements(SCHEMA)):
            if text.startswith(DEFINITIONS):
                with self.subTest(function=text[:60]):
                    self.assertIsNone(unqualified.search(text))

    def test_seed_matches_core_transitions(self):
        pairs = set()
        for text in render(lsql.upgrade_statements(SCHEMA)):
            if (
                text.lstrip().startswith("INSERT INTO")
                and "safeact_legal_transitions" in text
            ):
                pairs |= set(
                    re.findall(r"\('([A-Z_]+)', '([A-Z_]+)'\)", text)
                )
        self.assertEqual(
            pairs,
            {(a.value, b.value) for a, b in LEGAL_TRANSITIONS},
        )

    def test_core_transitions_equal_union_of_migration_seeds(self):
        core = {(a.value, b.value) for a, b in LEGAL_TRANSITIONS}
        self.assertEqual(core, set(lsql.SEEDED_TRANSITIONS))

    def test_grants_never_give_callers_table_writes(self):
        texts = render(lsql.disposable_grant_statements(
            SCHEMA,
            owner="safeact_fn_it_x",
            workers=("safeact_wk_it_x",),
            recoverers=("safeact_rc_it_x",),
        ))

        for text in texts:
            if "safeact_wk_it_x" in text or "safeact_rc_it_x" in text:
                with self.subTest(grant=text[:80]):
                    self.assertRegex(
                        text,
                        r"^GRANT (USAGE ON SCHEMA|EXECUTE ON FUNCTION"
                        r"|SELECT ON .*safeact_operation_current_state)",
                    )

    def test_provisioning_script_checks_every_lifecycle_object(self):
        script = (
            Path(__file__).resolve().parents[2]
            / "deploy/security/provision_local_admission_role.sql"
        ).read_text(encoding="utf-8")

        self.assertIn(f"'{lsql.HEAD_REVISION}'", script)

        for name, arguments in lsql.ALL_FUNCTIONS.items():
            with self.subTest(function=name):
                self.assertIn(f"'public.{name}({arguments})'", script)

        for relation in (*lsql.NEW_TABLES, lsql.CURRENT_STATE_VIEW):
            with self.subTest(relation=relation):
                self.assertIn(f"'{relation}'", script)

    def test_downgrade_drops_only_new_objects(self):
        texts = render(lsql.downgrade_statements(SCHEMA))

        for name in (
            "safeact_operations", "safeact_attempts",
            "safeact_audit_events", "safeact_outbox_events",
        ):
            with self.subTest(table=name):
                self.assertFalse(any(
                    t.startswith("DROP TABLE") and f'"{name}"' in t
                    for t in texts
                ))

        self.assertEqual(
            sum(t.startswith("DROP TABLE") for t in texts),
            len(lsql.NEW_TABLES),
        )


if __name__ == "__main__":
    unittest.main()
