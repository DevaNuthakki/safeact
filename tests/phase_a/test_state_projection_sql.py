"""Static checks on the 0017 current-state projection SQL."""

import re
import unittest

from storage import durable_lifecycle_sql as lsql
from storage.lifecycle_sql import v0003, v0017


SCHEMA = "safeact_it_" + "0" * 32


def render(statements):
    return [s.as_string(None).lstrip() for s in statements]


def view_columns(text):
    body = text.split(" AS\nSELECT", 1)[1].split("\nFROM", 1)[0]
    return [
        line.strip().rstrip(",").split(" AS ")[-1].split(".")[-1]
        for line in body.strip().splitlines()
    ]


class StateProjectionSqlTests(unittest.TestCase):

    def test_append_updates_the_projection_once_after_the_insert(self):
        text = render([v0017._append(v0003._schema(SCHEMA))])[0]
        insert = text.index(".safeact_operation_transitions (")
        update = text.index('UPDATE "' + SCHEMA + '"."safeact_state_projection"')
        audit = text.index(".safeact_audit_events (")
        self.assertLess(insert, update)
        self.assertLess(update, audit)
        self.assertEqual(text.count("safeact_state_projection"), 1)
        self.assertIn("WHERE p.operation_id = p_operation "
                      "AND p.seq = p_seq - 1", text)

    def test_view_keeps_the_0003_columns(self):
        s = v0003._schema(SCHEMA)
        before = render([v0003._view(s)])[0]
        after = render([v0017._view(s)])[0]
        self.assertEqual(view_columns(after), view_columns(before))
        self.assertNotIn("safeact_operation_transitions", after)

    def test_scans_no_longer_read_the_log_for_latest_state(self):
        for statement in (
            v0017._claimable_function, v0017._relay, v0017._in_state,
        ):
            text = render([statement(v0003._schema(SCHEMA))])[0]
            with self.subTest(function=text[:70]):
                self.assertNotIn("n.seq > t.seq", text)
                self.assertIn("safeact_state_projection", text)

    def test_trigger_functions_are_never_granted(self):
        texts = render(lsql.disposable_grant_statements(
            SCHEMA, owner="safeact_fn_it_x", workers=("safeact_wk_it_x",),
            recoverers=("safeact_rc_it_x",), advancers=("safeact_av_it_x",),
            apis=("safeact_ap_it_x",), mcps=("safeact_mc_it_x",),
        ))
        for name in (*lsql.TRIGGER_FUNCTIONS,
                     "safeact_state_projection_guard"):
            with self.subTest(function=name):
                self.assertFalse([
                    t for t in texts
                    if t.startswith("GRANT EXECUTE") and f'"{name}"' in t
                ])
        self.assertEqual(set(lsql.TRIGGER_FUNCTIONS),
                         {"safeact_state_projection_seed"})

    def test_guard_and_seed_are_attached_as_triggers(self):
        texts = render(v0017.upgrade_statements(SCHEMA))
        triggers = [t for t in texts if t.startswith("CREATE TRIGGER")]
        self.assertEqual(len(triggers), 2)
        self.assertTrue(any(
            "AFTER INSERT ON" in t and "safeact_operations " in t
            and "safeact_state_projection_seed()" in t for t in triggers
        ))
        self.assertTrue(any(
            "BEFORE INSERT OR UPDATE OR DELETE" in t
            and "safeact_state_projection_guard()" in t for t in triggers
        ))

    def test_backfill_runs_under_a_log_lock_before_the_guard(self):
        texts = render(v0017.upgrade_statements(SCHEMA))
        order = [
            next(i for i, t in enumerate(texts) if re.match(pattern, t))
            for pattern in (
                r"LOCK TABLE .*safeact_operation_transitions IN SHARE MODE",
                r'INSERT INTO .*"safeact_state_projection"',
                r"CREATE TRIGGER safeact_state_projection_write_guard",
                r"CREATE TRIGGER safeact_operations_state_projection_seed",
            )
        ]
        self.assertEqual(order[:2], sorted(order[:2]))
        self.assertLess(order[1], order[2])
        self.assertLess(order[1], order[3])

    def test_downgrade_restores_the_previous_texts(self):
        texts = render(v0017.downgrade_statements(SCHEMA))
        s = v0003._schema(SCHEMA)
        previous = render([
            v0003._view(s), v0003._functions(s)[0],
        ])
        self.assertEqual(texts[0], "CREATE OR REPLACE " + previous[0][7:])
        self.assertEqual(texts[1], "CREATE OR REPLACE " + previous[1][7:])
        self.assertTrue(texts[-1].startswith("DROP FUNCTION"))


if __name__ == "__main__":
    unittest.main()
