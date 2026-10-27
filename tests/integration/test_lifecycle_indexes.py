"""Durable lifecycle lookup and scan plans on seeded history.

Seeds 20,000 completed operations, 20 UNKNOWN, 20 VERIFYING and 10
never-claimed operations directly as the fixture superuser.

Asserted: current-state lookup by operation uses indexes; since 0017
every state scan (including CLAIMED and DISPATCHING, which every
operation passes through) and the claimable scan read the current-state
projection's (state, operation_id) index and never scan the transition
log.

Closed known limit (Phase B exit evidence): before 0017 the claimable
scan was an anti-join over the whole log and CLAIMED/DISPATCHING scans
read that state's full history. Measurements before and after are in
docs/phase-e/projection.md.
"""

import os
import unittest

from uuid import uuid4

from psycopg import sql

from core.lifecycle import LifecycleState as S
from storage import durable_lifecycle as L
from storage import durable_lifecycle_sql as lsql
from tests.integration.lifecycle_fixture import (
    DisposableLifecycleDatabase,
    commit,
)


COMPLETED = 20000
UNKNOWN = 20
VERIFYING = 20
UNCLAIMED = 10

@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class LifecycleIndexTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db = DisposableLifecycleDatabase()
        cls.addClassCleanup(cls.db.cleanup)
        cls.db.start()
        cls.schema = cls.db.schema

        cls.db.seed_history({
            "done": (COMPLETED, "SUCCEEDED", 100000),
            "unknown": (UNKNOWN, "UNKNOWN", 50000),
            "verifying": (VERIFYING, "VERIFYING", 50000),
            "fresh": (UNCLAIMED, None, 10),
        })

    # -- plan helpers --------------------------------------------------

    def plan_nodes(self, query):
        plan = self.db.admin.execute(
            sql.SQL("EXPLAIN (FORMAT JSON) ") + query
        ).fetchone()[0][0]["Plan"]

        nodes = []

        def walk(node):
            nodes.append((
                node["Node Type"],
                node.get("Relation Name"),
                node.get("Index Name"),
            ))
            for child in node.get("Plans", ()):
                walk(child)

        walk(plan)
        return nodes

    def assert_no_seq_scan(self, nodes, relation):
        self.assertNotIn(
            ("Seq Scan", relation),
            [(kind, rel) for kind, rel, _ in nodes],
            nodes,
        )

    def any_operation(self, prefix):
        return self.db.admin.execute(sql.SQL(
            "SELECT operation_id FROM {} WHERE request_key = %s"
        ).format(self.db.table("safeact_operations")),
            (prefix,),
        ).fetchone()[0]

    # -- tests -----------------------------------------------------------

    def test_current_state_lookup_uses_indexes(self):
        operation_id = self.any_operation("done-777")

        nodes = self.plan_nodes(sql.SQL(
            "SELECT * FROM {} WHERE operation_id = {}"
        ).format(
            self.db.table(lsql.CURRENT_STATE_VIEW),
            sql.Literal(str(operation_id)),
        ))

        self.assert_no_seq_scan(nodes, "safeact_operations")
        self.assert_no_seq_scan(nodes, "safeact_operation_transitions")

    def test_state_scans_use_the_projection_index(self):
        for state in (
            S.CLAIMED, S.DISPATCHING, S.UNKNOWN, S.VERIFYING,
            S.RETRY_ELIGIBLE, S.ESCALATED_UNRESOLVED,
        ):
            with self.subTest(state=state):
                nodes = self.plan_nodes(lsql.in_state_query(
                    self.schema, sql.Literal(state.value),
                    sql.Literal(100),
                ))
                self.assertNotIn(
                    "safeact_operation_transitions",
                    [rel for _, rel, _ in nodes],
                )
                self.assert_no_seq_scan(nodes, lsql.PROJECTION_TABLE)
                self.assertIn(
                    "ix_safeact_state_projection_state",
                    [index for _, _, index in nodes],
                )

    def test_claimable_scan_uses_the_projection_index(self):
        nodes = self.plan_nodes(
            lsql.claimable_query(self.schema, sql.Literal(100))
        )
        self.assert_no_seq_scan(nodes, "safeact_operation_transitions")
        self.assert_no_seq_scan(nodes, lsql.PROJECTION_TABLE)
        self.assertIn(
            "ix_safeact_state_projection_state",
            [index for _, _, index in nodes],
        )

    def test_state_scans_return_only_latest_state(self):
        recoverer = self.db.connect(self.db.recoverer)

        for state, expected in (
            (S.UNKNOWN, UNKNOWN),
            (S.VERIFYING, VERIFYING),
            (S.SUCCEEDED, COMPLETED),
        ):
            with self.subTest(state=state):
                rows = commit(recoverer, lambda: L.operations_in_state(
                    recoverer, self.schema, state, limit=1000,
                ))
                self.assertEqual(len(rows), min(expected, 1000))

    def test_claimable_returns_only_never_claimed_in_age_order(self):
        worker = self.db.connect(self.db.worker_a)

        claimable = commit(worker, lambda: L.claimable_operations(
            worker, self.schema, limit=1000,
        ))

        expected = tuple(
            self.any_operation(f"fresh-{n}")
            for n in range(1, UNCLAIMED + 1)
        )
        self.assertEqual(claimable, expected)

        first = commit(worker, lambda: L.claimable_operations(
            worker, self.schema, limit=3,
        ))
        self.assertEqual(first, expected[:3])

    def test_unknown_operation_lookup_returns_none(self):
        worker = self.db.connect(self.db.worker_a)
        self.assertIsNone(commit(worker, lambda: L.current_state(
            worker, self.schema, uuid4()
        )))


if __name__ == "__main__":
    unittest.main()
