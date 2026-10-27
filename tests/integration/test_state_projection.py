"""The current-state projection (0017) always equals the transition log.

Asserted, against a disposable schema at head:
- every operation has a projection row from the moment it is admitted,
  and after any sequence of protected calls (legal, illegal, stale,
  concurrent) the projection equals the log column by column;
- a refused call leaves the projection exactly as it was;
- the guard trigger refuses every write that disagrees with the log,
  even from the superuser and from the function owner (SAT29);
- when the log moves without the projection (a direct insert around
  the protected functions), the next protected call stops with SAT29;
- the claimable and in-state scans return exactly what the 0003/0007
  log queries return.

The randomized programs use Hypothesis with derandomize=True, so every
run replays the same examples.
"""

import os
import threading
import unittest

from uuid import uuid4

import psycopg

from hypothesis import HealthCheck, given, settings, strategies as st
from psycopg import sql

from core.lifecycle import LifecycleState as S
from storage import durable_lifecycle_sql as lsql
from storage.state_projection import projection_drift
from tests.integration.lifecycle_fixture import (
    DisposableLifecycleDatabase,
    commit,
)


ENABLED = (
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1"
)

DRIFT = lsql.PROJECTION_DRIFT
STATES = [state.value for state in S]


def call(connection, schema, name, arguments):
    placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in arguments)
    return connection.execute(
        sql.SQL("SELECT * FROM {}.{}({})").format(
            sql.Identifier(schema), sql.Identifier(name), placeholders,
        ),
        arguments,
    ).fetchall()


@unittest.skipUnless(ENABLED, "Explicit disposable PostgreSQL opt-in required")
class StateProjectionTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db = DisposableLifecycleDatabase()
        cls.addClassCleanup(cls.db.cleanup)
        cls.db.start()
        cls.schema = cls.db.schema
        cls.worker = cls.db.connect(cls.db.worker_a)
        cls.other = cls.db.connect(cls.db.worker_b)
        cls.recoverer = cls.db.connect(cls.db.recoverer)

    # -- helpers -------------------------------------------------------

    def admit(self):
        return self.db.admit(request_key=f"projection-{uuid4().hex}")

    def row(self, operation_id):
        return self.db.admin.execute(sql.SQL(
            "SELECT seq, state, claim_id, attempt_id, actor, "
            "lease_expires_at, state_changed_at FROM {} "
            "WHERE operation_id = %s"
        ).format(self.db.table(lsql.PROJECTION_TABLE)),
            (operation_id,),
        ).fetchone()

    def assert_matches_log(self):
        self.assertEqual(projection_drift(self.db.admin, self.schema), [])

    def assert_drift_error(self, action):
        with self.assertRaises(psycopg.Error) as caught:
            action()
        self.assertEqual(caught.exception.sqlstate, DRIFT)

    # -- every path keeps it equal ---------------------------------------

    def test_admission_writes_the_recorded_row(self):
        operation_id = self.admit()

        seq, state, claim, attempt, actor, lease, changed = self.row(
            operation_id
        )
        self.assertEqual((seq, state), (0, "RECORDED"))
        self.assertEqual((claim, attempt, actor, lease, changed),
                         (None,) * 5)
        self.assert_matches_log()

    def test_every_driven_state_matches_the_log(self):
        for target in (
            S.RECORDED, S.CLAIMED, S.DISPATCHING, S.SUCCEEDED,
            S.FAILED_CONFIRMED, S.UNKNOWN, S.VERIFYING,
            S.CANCELLED_BEFORE_CLAIM,
        ):
            with self.subTest(target=target):
                operation_id = self.admit()
                driven = self.db.drive_to(
                    operation_id, target,
                    worker=self.worker, recoverer=self.recoverer,
                )
                seq, state, *_ = self.row(operation_id)
                self.assertEqual((seq, state), (driven.seq, target.value))
                self.assert_matches_log()

    def test_a_refused_call_leaves_the_projection_unchanged(self):
        operation_id = self.admit()
        driven = self.db.drive_to(
            operation_id, S.CLAIMED,
            worker=self.worker, recoverer=self.recoverer,
        )
        before = self.row(operation_id)

        # Stale seq, wrong owner, illegal target: each is refused.
        for connection, name, arguments in (
            (self.worker, "safeact_begin_dispatch",
             (operation_id, driven.seq + 1, driven.claim.claim_id, 60)),
            (self.other, "safeact_begin_dispatch",
             (operation_id, driven.seq, driven.claim.claim_id, 60)),
            (self.recoverer, "safeact_recovery_transition",
             (operation_id, driven.seq, "CLAIMED", "SUCCEEDED",
              "fixture_recovery", "{}")),
        ):
            with self.subTest(name=name):
                with self.assertRaises(psycopg.Error):
                    commit(connection, lambda: call(
                        connection, self.schema, name, arguments
                    ))
                self.assertEqual(self.row(operation_id), before)
        self.assert_matches_log()

    # -- the guard -------------------------------------------------------

    def projection_write(self, statement, arguments, *, role=None):
        admin = self.db.admin

        def write():
            with admin.transaction():
                if role is not None:
                    admin.execute(sql.SQL("SET LOCAL ROLE {}").format(
                        sql.Identifier(role)
                    ))
                admin.execute(statement.format(
                    p=self.db.table(lsql.PROJECTION_TABLE)
                ), arguments)
        return write

    def test_guard_refuses_writes_that_disagree_with_the_log(self):
        operation_id = self.admit()
        driven = self.db.drive_to(
            operation_id, S.DISPATCHING,
            worker=self.worker, recoverer=self.recoverer,
        )
        before = self.row(operation_id)

        writes = {
            "skip a seq": ("UPDATE {p} SET seq = seq + 2, "
                           "state = 'UNKNOWN' WHERE operation_id = %s"),
            "seq without a transition": (
                "UPDATE {p} SET seq = seq + 1, state = 'UNKNOWN' "
                "WHERE operation_id = %s"),
            "rewrite the current state": (
                "UPDATE {p} SET state = 'SUCCEEDED' "
                "WHERE operation_id = %s"),
            "move back": ("UPDATE {p} SET seq = 0, state = 'RECORDED', "
                          "claim_id = NULL, attempt_id = NULL "
                          "WHERE operation_id = %s"),
            "delete": "DELETE FROM {p} WHERE operation_id = %s",
        }
        for role in (None, self.db.owner):
            for label, statement in writes.items():
                with self.subTest(role=role or "superuser", write=label):
                    write = self.projection_write(
                        sql.SQL(statement), (operation_id,), role=role,
                    )
                    if role is not None and label == "delete":
                        # The owner was never granted DELETE at all.
                        with self.assertRaises(
                            psycopg.errors.InsufficientPrivilege
                        ):
                            write()
                    else:
                        self.assert_drift_error(write)
                    self.assertEqual(self.row(operation_id), before)

        other = self.admit()
        with self.subTest(write="second insert"):
            with self.assertRaises(psycopg.errors.UniqueViolation):
                self.projection_write(sql.SQL(
                    "INSERT INTO {p} (operation_id, seq, state) "
                    "VALUES (%s, 0, 'RECORDED')"
                ), (other,))()
        self.assertEqual(driven.seq, before[0])
        self.assert_matches_log()

    def test_callers_cannot_touch_the_projection(self):
        for connection in (self.worker, self.recoverer):
            for statement in (
                "SELECT 1 FROM {p}", "UPDATE {p} SET seq = seq",
                "DELETE FROM {p}",
            ):
                with self.subTest(statement=statement):
                    with self.assertRaises(
                        psycopg.errors.InsufficientPrivilege
                    ):
                        try:
                            connection.execute(sql.SQL(statement).format(
                                p=self.db.table(lsql.PROJECTION_TABLE)
                            ))
                        finally:
                            connection.rollback()

    def test_lock_stops_when_the_log_moves_without_the_projection(self):
        operation_id = self.admit()

        # Around the protected functions on purpose, as the superuser.
        self.db.admin.execute(sql.SQL("""
            INSERT INTO {} (operation_id, seq, from_state, to_state,
                            claim_id, actor, lease_expires_at, reason)
            VALUES (%s, 1, 'RECORDED', 'CLAIMED', gen_random_uuid(),
                    'outside', now() + interval '1 minute', 'outside')
        """).format(self.db.table("safeact_operation_transitions")),
            (operation_id,))
        self.assertEqual(
            [d.operation_id for d in projection_drift(
                self.db.admin, self.schema)],
            [operation_id],
        )

        self.assert_drift_error(lambda: commit(self.worker, lambda: call(
            self.worker, self.schema, "safeact_claim", (operation_id, 60)
        )))
        self.assert_drift_error(lambda: commit(self.recoverer, lambda: call(
            self.recoverer, self.schema, "safeact_recovery_transition",
            (operation_id, 1, "CLAIMED", "UNKNOWN", "fixture_recovery", "{}"),
        )))

        self.db.catch_up_projection()
        self.assert_matches_log()

    # -- scans -----------------------------------------------------------

    def test_scans_equal_the_log_queries(self):
        for target in (S.CLAIMED, S.DISPATCHING, S.UNKNOWN, S.VERIFYING,
                       S.SUCCEEDED, S.CANCELLED_BEFORE_CLAIM):
            self.db.drive_to(
                self.admit(), target,
                worker=self.worker, recoverer=self.recoverer,
            )
        self.admit()
        admin, limit = self.db.admin, sql.Literal(1000)

        self.assertEqual(
            admin.execute(lsql.claimable_query(self.schema, limit))
            .fetchall(),
            admin.execute(lsql.log_claimable_query(self.schema, limit))
            .fetchall(),
        )
        for state in STATES:
            if state == "RECORDED":
                continue
            with self.subTest(state=state):
                value = sql.Literal(state)
                self.assertEqual(
                    admin.execute(lsql.in_state_query(
                        self.schema, value, limit)).fetchall(),
                    admin.execute(lsql.log_in_state_query(
                        self.schema, value, limit)).fetchall(),
                )

    # -- concurrency -----------------------------------------------------

    def test_concurrent_claims_admit_one_and_keep_it_equal(self):
        operations = [self.admit() for _ in range(4)]
        connections = [self.db.connect(self.db.worker_a) for _ in range(6)]
        results = {op: [] for op in operations}
        start = threading.Barrier(len(connections))

        def race(connection):
            start.wait()
            for operation_id in operations:
                try:
                    commit(connection, lambda: call(
                        connection, self.schema, "safeact_claim",
                        (operation_id, 60),
                    ))
                    results[operation_id].append("won")
                except psycopg.Error as error:
                    results[operation_id].append(error.sqlstate)

        threads = [threading.Thread(target=race, args=(c,))
                   for c in connections]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        for operation_id in operations:
            with self.subTest(operation=operation_id):
                self.assertEqual(results[operation_id].count("won"), 1)
                self.assertEqual(self.row(operation_id)[:2], (1, "CLAIMED"))
        self.assert_matches_log()

    # -- randomized programs ---------------------------------------------

    @settings(
        max_examples=25, deadline=None, derandomize=True, database=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(st.lists(
        st.tuples(
            st.integers(0, 2),
            st.sampled_from(("claim", "dispatch", "proof", "outcome",
                             "recover")),
            st.sampled_from((0, 0, 0, 1, -1)),
            st.sampled_from(STATES),
            st.booleans(),
        ),
        min_size=1, max_size=14,
    ))
    def test_random_programs_keep_the_projection_equal(self, program):
        operations = [self.admit() for _ in range(3)]

        for index, action, offset, target, applied in program:
            operation_id = operations[index]
            before = self.row(operation_id)
            seq, state, claim, attempt = before[:4]
            connection, name, arguments = {
                "claim": (self.worker, "safeact_claim", (operation_id, 60)),
                "dispatch": (self.worker, "safeact_begin_dispatch",
                             (operation_id, seq + offset, claim, 60)),
                "proof": (self.worker, "safeact_record_attempt_outcome", (
                    attempt,
                    "confirmed_applied" if applied else "proven_not_applied",
                    "applied" if applied else "proven_not_applied",
                    f"proof-{uuid4().hex}", "{}",
                )),
                "outcome": (self.worker, "safeact_record_dispatch_outcome", (
                    operation_id, seq + offset, claim, target,
                    "ambiguous_no_response" if target == "UNKNOWN"
                    else "fixture_outcome", "{}",
                )),
                "recover": (self.recoverer, "safeact_recovery_transition", (
                    operation_id, seq + offset, state, target,
                    "fixture_recovery", "{}",
                )),
            }[action]

            try:
                commit(connection, lambda: call(
                    connection, self.schema, name, arguments
                ))
                refused = False
            except psycopg.Error:
                refused = True

            if refused or action == "proof":
                self.assertEqual(self.row(operation_id), before)
            else:
                self.assertEqual(self.row(operation_id)[0], seq + 1)
            self.assert_matches_log()


if __name__ == "__main__":
    unittest.main()
