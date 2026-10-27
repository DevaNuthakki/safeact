"""Durable lifecycle transition enforcement (acceptance check A2).

Disposable schema only. No provider exists here: these tests prove
which database transitions are possible, not that any dispatch is.
"""

import os
import time
import unittest

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg
from psycopg import sql

from core.attempts import AttemptOutcome
from core.contracts import EvidenceKind
from core.lifecycle import (
    DISPATCH_OUTCOMES,
    LEGAL_TRANSITIONS,
    RECOVERY_TRANSITIONS,
    LifecycleState as S,
)
from storage import durable_lifecycle as L
from storage.submission import SubmissionConflict, submit_intent
from tests.integration.lifecycle_fixture import (
    DisposableLifecycleDatabase,
    commit,
)


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class LifecycleTransitionTests(unittest.TestCase):

    def setUp(self):
        self.db = DisposableLifecycleDatabase()
        self.addCleanup(self.db.cleanup)
        self.db.start()

        self.schema = self.db.schema
        self.worker = self.db.connect(self.db.worker_a)
        self.other_worker = self.db.connect(self.db.worker_b)
        self.recoverer = self.db.connect(self.db.recoverer)
        self._keys = 0

    # -- helpers -----------------------------------------------------

    def admit(self):
        self._keys += 1
        return self.db.admit(request_key=f"lifecycle-{self._keys}")

    def at(self, state):
        operation_id = self.admit()
        return self.db.drive_to(
            operation_id, state,
            worker=self.worker, recoverer=self.recoverer,
        )

    def state(self, operation_id):
        current = commit(self.worker, lambda: L.current_state(
            self.worker, self.schema, operation_id
        ))
        return current.state, current.seq

    def rejected(self, connection, error, action):
        with self.assertRaises(error):
            commit(connection, action)

    def transitions(self, operation_id):
        return self.db.count(
            "safeact_operation_transitions", operation_id=operation_id
        )

    # -- schema and happy chain --------------------------------------

    def test_seed_matches_core_legal_transitions(self):
        rows = self.db.admin.execute(sql.SQL(
            "SELECT from_state, to_state FROM {}"
        ).format(self.db.table("safeact_legal_transitions"))).fetchall()

        self.assertEqual(
            set(rows),
            {(a.value, b.value) for a, b in LEGAL_TRANSITIONS},
        )

    def test_happy_chain_appends_transition_and_audit_per_step(self):
        driven = self.at(S.SUCCEEDED)
        operation_id = driven.operation_id

        self.assertEqual(self.state(operation_id), (S.SUCCEEDED, 3))

        rows = self.db.admin.execute(sql.SQL(
            "SELECT seq, from_state, to_state, actor, claim_id "
            "FROM {} WHERE operation_id = %s ORDER BY seq"
        ).format(self.db.table("safeact_operation_transitions")),
            (operation_id,),
        ).fetchall()

        self.assertEqual(
            [(r[0], r[1], r[2]) for r in rows],
            [
                (1, "RECORDED", "CLAIMED"),
                (2, "CLAIMED", "DISPATCHING"),
                (3, "DISPATCHING", "SUCCEEDED"),
            ],
        )
        self.assertEqual({r[3] for r in rows}, {self.db.worker_a})
        self.assertEqual({r[4] for r in rows}, {driven.claim.claim_id})

        audit = self.db.admin.execute(sql.SQL(
            "SELECT event_type, actor_id FROM {} "
            "WHERE operation_id = %s ORDER BY audit_id"
        ).format(self.db.table("safeact_audit_events")),
            (operation_id,),
        ).fetchall()

        self.assertEqual(
            [event for event, _ in audit],
            [
                "INTENT_RECORDED",
                "LIFECYCLE_CLAIMED",
                "DISPATCH_RISK_COMMITTED",
                "ATTEMPT_EVIDENCE_RECORDED",
                "LIFECYCLE_DISPATCH_OUTCOME",
            ],
        )
        self.assertEqual(self.db.count(
            "safeact_attempts", operation_id=operation_id
        ), 1)

    def test_rolled_back_transition_leaves_no_transition_or_audit(self):
        operation_id = self.admit()

        L.claim(self.worker, self.schema, operation_id, lease_seconds=60)
        self.worker.rollback()

        self.assertEqual(self.transitions(operation_id), 0)
        self.assertEqual(self.db.count(
            "safeact_audit_events", operation_id=operation_id
        ), 1)
        self.assertEqual(self.state(operation_id), (S.RECORDED, 0))

    # -- A2: illegal, skipped and stale transitions ------------------

    def test_every_illegal_recovery_pair_is_rejected(self):
        for current in S:
            if current is S.RECORDED:
                continue

            driven = self.at(current)

            for target in S:
                if (current, target) in RECOVERY_TRANSITIONS:
                    continue
                with self.subTest(current=current, target=target):
                    self.rejected(
                        self.recoverer, L.IllegalTransition,
                        lambda: L.recovery_transition(
                            self.recoverer, self.schema,
                            driven.operation_id,
                            expected_seq=driven.seq,
                            expected_state=current,
                            to_state=target, reason="illegal",
                        ),
                    )

            self.assertEqual(
                self.state(driven.operation_id)[1], driven.seq
            )

    def test_dispatch_outcome_only_from_dispatching(self):
        for current in (S.CLAIMED, S.SUCCEEDED, S.UNKNOWN):
            driven = self.at(current)
            fence = L.DispatchFence(
                driven.operation_id,
                driven.claim.claim_id,
                uuid4(),
                driven.seq,
            )
            for target in DISPATCH_OUTCOMES:
                with self.subTest(current=current, target=target):
                    self.rejected(
                        self.worker, L.IllegalTransition,
                        lambda: L.record_dispatch_outcome(
                            self.worker, self.schema, fence, target,
                            reason="illegal",
                        ),
                    )

        driven = self.at(S.DISPATCHING)
        for target in (S.VERIFYING, S.RETRY_ELIGIBLE, S.CLAIMED):
            with self.subTest(target=target):
                self.rejected(
                    self.worker, L.IllegalTransition,
                    lambda: L.record_dispatch_outcome(
                        self.worker, self.schema, driven.fence,
                        target, reason="illegal",
                    ),
                )

    def test_skipped_states_are_rejected(self):
        operation_id = self.admit()
        unclaimed = L.Claim(operation_id, uuid4(), 0)

        # RECORDED -> DISPATCHING without a claim.
        self.rejected(
            self.worker, L.IllegalTransition,
            lambda: L.begin_dispatch(
                self.worker, self.schema, unclaimed, lease_seconds=60,
            ),
        )

        # RECORDED -> SUCCEEDED.
        self.rejected(
            self.worker, L.IllegalTransition,
            lambda: L.record_dispatch_outcome(
                self.worker, self.schema,
                L.DispatchFence(operation_id, uuid4(), uuid4(), 0),
                S.SUCCEEDED, reason="skip",
            ),
        )

        # CLAIMED -> SUCCEEDED.
        claimed = self.at(S.CLAIMED)
        self.rejected(
            self.worker, L.IllegalTransition,
            lambda: L.record_dispatch_outcome(
                self.worker, self.schema,
                L.DispatchFence(
                    claimed.operation_id, claimed.claim.claim_id,
                    uuid4(), claimed.seq,
                ),
                S.SUCCEEDED, reason="skip",
            ),
        )

        # UNKNOWN -> SUCCEEDED without VERIFYING.
        unknown = self.at(S.UNKNOWN)
        self.rejected(
            self.recoverer, L.IllegalTransition,
            lambda: L.recovery_transition(
                self.recoverer, self.schema, unknown.operation_id,
                expected_seq=unknown.seq, expected_state=S.UNKNOWN,
                to_state=S.SUCCEEDED, reason="skip",
            ),
        )

        self.assertEqual(self.transitions(operation_id), 0)

    def test_stale_owner_version_is_rejected(self):
        driven = self.at(S.DISPATCHING)
        stale_claim = L.Claim(
            driven.operation_id, driven.claim.claim_id, 0
        )

        self.rejected(
            self.worker, L.StaleTransition,
            lambda: L.begin_dispatch(
                self.worker, self.schema, stale_claim,
                lease_seconds=60,
            ),
        )

        self.db.prove(self.worker, driven.fence.attempt_id, applied=True)
        commit(self.worker, lambda: L.record_dispatch_outcome(
            self.worker, self.schema, driven.fence, S.SUCCEEDED,
            reason="provider_confirmed",
        ))

        # Replaying the same fence after success is stale.
        self.rejected(
            self.worker, L.StaleTransition,
            lambda: L.record_dispatch_outcome(
                self.worker, self.schema, driven.fence,
                S.FAILED_CONFIRMED, reason="replay",
            ),
        )

        verifying = self.at(S.VERIFYING)
        for seq, state in (
            (verifying.seq - 1, S.VERIFYING),
            (verifying.seq, S.UNKNOWN),
        ):
            with self.subTest(seq=seq, state=state):
                self.rejected(
                    self.recoverer, L.StaleTransition,
                    lambda: L.recovery_transition(
                        self.recoverer, self.schema,
                        verifying.operation_id,
                        expected_seq=seq, expected_state=state,
                        to_state=S.SUCCEEDED, reason="stale",
                    ),
                )

        self.assertEqual(
            self.state(driven.operation_id), (S.SUCCEEDED, 3)
        )

    def test_claim_of_claimed_operation_is_stale(self):
        driven = self.at(S.CLAIMED)

        self.rejected(
            self.other_worker, L.StaleTransition,
            lambda: L.claim(
                self.other_worker, self.schema,
                driven.operation_id, lease_seconds=60,
            ),
        )

    def test_wrong_claim_owner_is_rejected(self):
        driven = self.at(S.CLAIMED)

        # Another worker presenting the real claim.
        self.rejected(
            self.other_worker, L.NotClaimOwner,
            lambda: L.begin_dispatch(
                self.other_worker, self.schema, driven.claim,
                lease_seconds=60,
            ),
        )

        # The owner presenting a forged claim id.
        forged = L.Claim(driven.operation_id, uuid4(), driven.seq)
        self.rejected(
            self.worker, L.NotClaimOwner,
            lambda: L.begin_dispatch(
                self.worker, self.schema, forged, lease_seconds=60,
            ),
        )

        dispatching = self.at(S.DISPATCHING)
        self.rejected(
            self.other_worker, L.NotClaimOwner,
            lambda: L.record_dispatch_outcome(
                self.other_worker, self.schema, dispatching.fence,
                S.SUCCEEDED, reason="not_mine",
            ),
        )

        self.assertEqual(self.state(driven.operation_id)[1], 1)

    def test_unknown_operation_is_rejected(self):
        self.rejected(
            self.worker, L.UnknownOperation,
            lambda: L.claim(
                self.worker, self.schema, uuid4(), lease_seconds=60,
            ),
        )

    def test_invalid_lease_reason_and_evidence_are_rejected(self):
        operation_id = self.admit()

        for lease in (0, -1, 3601):
            with self.subTest(lease=lease):
                with self.assertRaises(psycopg.errors.InvalidParameterValue):
                    commit(self.worker, lambda: L.claim(
                        self.worker, self.schema, operation_id,
                        lease_seconds=lease,
                    ))

        driven = self.at(S.DISPATCHING)
        with self.assertRaises(psycopg.errors.InvalidParameterValue):
            commit(self.worker, lambda: L.record_dispatch_outcome(
                self.worker, self.schema, driven.fence, S.SUCCEEDED,
                reason="",
            ))

        with self.assertRaises(TypeError):
            L.record_dispatch_outcome(
                self.worker, self.schema, driven.fence, S.SUCCEEDED,
                reason="ok", evidence=["not", "an", "object"],
            )

    # -- PK, FK and immutability backstops ---------------------------

    def admin_insert_transition(self, operation_id, seq, a, b):
        self.db.admin.execute(sql.SQL(
            "INSERT INTO {} (operation_id, seq, from_state, to_state, "
            "claim_id, actor, lease_expires_at, reason) "
            "VALUES (%s, %s, %s, %s, %s, 'admin', now(), 'direct')"
        ).format(self.db.table("safeact_operation_transitions")),
            (operation_id, seq, a, b, uuid4()),
        )

    def test_primary_key_rejects_two_rows_for_same_version(self):
        driven = self.at(S.CLAIMED)

        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.admin_insert_transition(
                driven.operation_id, 1, "RECORDED", "CLAIMED"
            )

        self.assertEqual(self.transitions(driven.operation_id), 1)

    def test_direct_illegal_pair_and_bad_seq_rejected_by_table(self):
        operation_id = self.admit()

        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.admin_insert_transition(
                operation_id, 1, "RECORDED", "SUCCEEDED"
            )

        with self.assertRaises(psycopg.errors.CheckViolation):
            self.admin_insert_transition(
                operation_id, 2, "RECORDED", "CLAIMED"
            )

        self.assertEqual(self.transitions(operation_id), 0)

    def test_history_rows_cannot_be_updated_or_deleted(self):
        driven = self.at(S.SUCCEEDED)

        for table in (
            "safeact_operation_transitions",
            "safeact_legal_transitions",
            "safeact_attempts",
        ):
            for statement in (
                "UPDATE {} SET to_state = to_state"
                if table != "safeact_attempts"
                else "UPDATE {} SET ordinal = ordinal",
                "DELETE FROM {}",
            ):
                with self.subTest(table=table, statement=statement):
                    with self.assertRaises(
                        psycopg.errors.CheckViolation
                    ):
                        self.db.admin.execute(
                            sql.SQL(statement).format(
                                self.db.table(table)
                            )
                        )

        self.assertEqual(self.transitions(driven.operation_id), 3)

    # -- concurrency -------------------------------------------------

    def race(self, contenders, action):
        barrier = Barrier(len(contenders))

        def run(connection):
            barrier.wait(timeout=10)
            try:
                commit(connection, lambda: action(connection))
                return "won"
            except L.StaleTransition:
                return "stale"

        with ThreadPoolExecutor(max_workers=len(contenders)) as pool:
            return list(pool.map(run, contenders))

    def test_eight_concurrent_claims_produce_one_owner(self):
        operation_id = self.admit()
        contenders = [
            self.db.connect(
                self.db.worker_a if i % 2 else self.db.worker_b
            )
            for i in range(8)
        ]

        results = self.race(contenders, lambda c: L.claim(
            c, self.schema, operation_id, lease_seconds=60,
        ))

        self.assertEqual(results.count("won"), 1)
        self.assertEqual(results.count("stale"), 7)
        self.assertEqual(self.transitions(operation_id), 1)

    def test_eight_concurrent_outcomes_for_same_version(self):
        driven = self.at(S.DISPATCHING)
        contenders = [self.db.connect(self.db.worker_a) for _ in range(8)]
        results = self.race(contenders, lambda c: L.record_dispatch_outcome(
            c, self.schema, driven.fence, S.UNKNOWN,
            reason="ambiguous_no_response",
        ))

        self.assertEqual(results.count("won"), 1)
        self.assertEqual(results.count("stale"), 7)
        self.assertEqual(self.transitions(driven.operation_id), 3)

    # -- leases --------------------------------------------------------

    def test_dispatch_investigation_waits_for_lease_expiry(self):
        operation_id = self.admit()
        held = commit(self.worker, lambda: L.claim(
            self.worker, self.schema, operation_id, lease_seconds=60,
        ))
        fence = commit(self.worker, lambda: L.begin_dispatch(
            self.worker, self.schema, held, lease_seconds=1,
        ))

        investigate = lambda: L.recovery_transition(  # noqa: E731
            self.recoverer, self.schema, operation_id,
            expected_seq=fence.seq, expected_state=S.DISPATCHING,
            to_state=S.UNKNOWN, reason="lease_expired",
        )

        self.rejected(self.recoverer, L.LeaseNotExpired, investigate)

        time.sleep(1.2)
        commit(self.recoverer, investigate)

        self.assertEqual(self.state(operation_id), (S.UNKNOWN, 3))

        # The stale worker can no longer record an outcome over it.
        self.rejected(
            self.worker, L.StaleTransition,
            lambda: L.record_dispatch_outcome(
                self.worker, self.schema, fence, S.SUCCEEDED,
                reason="late_reply",
            ),
        )

    def test_expired_claim_lease_cannot_start_dispatch(self):
        operation_id = self.admit()
        held = commit(self.worker, lambda: L.claim(
            self.worker, self.schema, operation_id, lease_seconds=1,
        ))

        time.sleep(1.2)

        self.rejected(
            self.worker, L.ClaimLeaseExpired,
            lambda: L.begin_dispatch(
                self.worker, self.schema, held, lease_seconds=60,
            ),
        )
        self.assertEqual(self.db.count(
            "safeact_attempts", operation_id=operation_id
        ), 0)
        self.assertEqual(self.state(operation_id), (S.CLAIMED, 1))

    # -- reclaim of retry-eligible work (0007) ------------------------

    def test_retry_eligible_is_claimable_and_reclaimed_once(self):
        retry = self.at(S.RETRY_ELIGIBLE)
        fresh = self.admit()

        claimable = commit(self.worker, lambda: L.claimable_operations(
            self.worker, self.schema, limit=100,
        ))
        self.assertEqual(set(claimable), {retry.operation_id, fresh})

        held = commit(self.worker, lambda: L.claim(
            self.worker, self.schema, retry.operation_id, lease_seconds=60,
        ))
        self.assertEqual(held.seq, retry.seq + 1)
        self.assertEqual(
            self.state(retry.operation_id), (S.CLAIMED, retry.seq + 1)
        )

        self.rejected(
            self.other_worker, L.StaleTransition,
            lambda: L.claim(
                self.other_worker, self.schema,
                retry.operation_id, lease_seconds=60,
            ),
        )
        self.assertEqual(self.db.count(
            "safeact_attempts", operation_id=retry.operation_id
        ), 1)

    def test_current_state_view_accounts_for_every_operation(self):
        expected = {}
        for state in S:
            driven = self.at(state)
            expected[driven.operation_id] = state

        rows = self.db.admin.execute(sql.SQL(
            "SELECT operation_id, state FROM {}"
        ).format(self.db.table("safeact_operation_current_state"))
        ).fetchall()

        self.assertEqual(
            {operation_id: S(state) for operation_id, state in rows},
            expected,
        )

    def test_claim_requires_complete_admission_bundle(self):
        operation_id = uuid4()
        self.db.admin.execute(sql.SQL(
            "INSERT INTO {} (operation_id, tenant_id, request_key, "
            "action_type, resource_id, provider_account_id, "
            "connector_id, contract_version, payload_sha256, "
            "payload_canonical) VALUES (%s, 't', 'no-outbox', 'a', "
            "'r', 'p', 'c', 'v1', "
            "encode(sha256(convert_to('{{}}', 'UTF8')), 'hex'), '{{}}')"
        ).format(self.db.table("safeact_operations")), (operation_id,))

        self.rejected(
            self.worker, L.AdmissionIncomplete,
            lambda: L.claim(
                self.worker, self.schema, operation_id,
                lease_seconds=60,
            ),
        )

    # -- evidence and deliveries -------------------------------------

    def test_late_evidence_is_appended_without_changing_decision(self):
        driven = self.at(S.ESCALATED_UNRESOLVED)

        commit(self.worker, lambda: L.record_attempt_outcome(
            self.worker, self.schema, driven.fence.attempt_id,
            AttemptOutcome.CONFIRMED_APPLIED,
            evidence_kind=EvidenceKind.APPLIED,
            reference="late-provider-receipt",
        ))

        self.assertEqual(
            self.state(driven.operation_id),
            (S.ESCALATED_UNRESOLVED, driven.seq),
        )

        observed = self.db.admin.execute(sql.SQL(
            "SELECT observed_by, observed_seq, outcome FROM {}"
        ).format(self.db.table("safeact_attempt_outcomes"))).fetchall()
        self.assertEqual(observed, [
            (self.db.worker_a, driven.seq, "confirmed_applied"),
        ])

    def test_decisive_evidence_requires_reference(self):
        driven = self.at(S.DISPATCHING)

        with self.assertRaises(psycopg.errors.CheckViolation):
            commit(self.worker, lambda: L.record_attempt_outcome(
                self.worker, self.schema, driven.fence.attempt_id,
                AttemptOutcome.PROVEN_NOT_APPLIED,
                evidence_kind=EvidenceKind.PROVEN_NOT_APPLIED,
            ))

    def test_outbox_may_be_delivered_more_than_once(self):
        operation_id = self.admit()
        outbox_id = self.db.outbox_id(operation_id)

        first = commit(self.worker, lambda: L.record_outbox_delivery(
            self.worker, self.schema, outbox_id
        ))
        second = commit(self.other_worker, lambda: L.record_outbox_delivery(
            self.other_worker, self.schema, outbox_id
        ))

        self.assertNotEqual(first, second)
        self.assertEqual(self.db.count(
            "safeact_outbox_deliveries", outbox_id=outbox_id
        ), 2)
        self.assertEqual(self.state(operation_id), (S.RECORDED, 0))

    # -- outcome evidence gates (deviation from the 05 diagram) -------

    def record(self, attempt_id, outcome, kind, reference=None):
        commit(self.worker, lambda: L.record_attempt_outcome(
            self.worker, self.schema, attempt_id, outcome,
            evidence_kind=kind, reference=reference,
        ))

    def to_failed_from_verifying(self, driven):
        return lambda: L.recovery_transition(
            self.recoverer, self.schema, driven.operation_id,
            expected_seq=driven.seq, expected_state=S.VERIFYING,
            to_state=S.FAILED_CONFIRMED, reason="lookup_result",
        )

    def test_empty_or_inconclusive_lookup_never_reaches_failed(self):
        driven = self.at(S.VERIFYING)
        attempt = driven.fence.attempt_id
        attempt_to_failed = self.to_failed_from_verifying(driven)

        # Empty lookup: nothing recorded at all.
        self.rejected(
            self.recoverer, L.EvidenceRequired, attempt_to_failed
        )

        for kind in (EvidenceKind.NOT_FOUND, EvidenceKind.INCONCLUSIVE):
            with self.subTest(kind=kind):
                self.record(
                    attempt, AttemptOutcome.MAY_HAVE_DISPATCHED, kind
                )
                self.rejected(
                    self.recoverer, L.EvidenceRequired,
                    attempt_to_failed,
                )

        # A "not applied" outcome without its decisive evidence kind.
        self.record(
            attempt, AttemptOutcome.PROVEN_NOT_APPLIED,
            EvidenceKind.NOT_FOUND, reference="lookup-empty",
        )
        self.rejected(
            self.recoverer, L.EvidenceRequired, attempt_to_failed
        )

        self.assertEqual(
            self.state(driven.operation_id), (S.VERIFYING, driven.seq)
        )

    def test_failed_confirmed_from_verifying_with_authoritative_proof(self):
        driven = self.at(S.VERIFYING)
        self.db.prove(
            self.worker, driven.fence.attempt_id, applied=False,
            reference="provider-says-never-applied",
        )

        commit(self.recoverer, self.to_failed_from_verifying(driven))

        self.assertEqual(
            self.state(driven.operation_id),
            (S.FAILED_CONFIRMED, driven.seq + 1),
        )

    def test_contradictory_evidence_never_reaches_failed(self):
        driven = self.at(S.VERIFYING)
        attempt = driven.fence.attempt_id
        self.db.prove(self.worker, attempt, applied=False)
        self.db.prove(self.worker, attempt, applied=True)

        self.rejected(
            self.recoverer, L.EvidenceRequired,
            self.to_failed_from_verifying(driven),
        )

    def test_dispatch_outcomes_require_matching_evidence(self):
        for target, wrong in (
            (S.FAILED_CONFIRMED, True),
            (S.SUCCEEDED, False),
        ):
            with self.subTest(target=target):
                driven = self.at(S.DISPATCHING)
                claim_outcome = lambda: L.record_dispatch_outcome(  # noqa
                    self.worker, self.schema, driven.fence, target,
                    reason="provider_reply",
                )

                self.rejected(
                    self.worker, L.EvidenceRequired, claim_outcome
                )
                self.record(
                    driven.fence.attempt_id,
                    AttemptOutcome.MAY_HAVE_DISPATCHED,
                    EvidenceKind.NOT_FOUND,
                )
                self.rejected(
                    self.worker, L.EvidenceRequired, claim_outcome
                )

                self.db.prove(
                    self.worker, driven.fence.attempt_id, applied=wrong
                )
                self.rejected(
                    self.worker, L.EvidenceRequired, claim_outcome
                )

                self.assertEqual(
                    self.state(driven.operation_id),
                    (S.DISPATCHING, driven.seq),
                )

    # -- A2: changed-payload key reuse -------------------------------

    def test_changed_payload_key_reuse_is_rejected(self):
        self.db.admit(request_key="reused-key")

        with self.assertRaises(SubmissionConflict):
            submit_intent(self.db.engine, self.db.request(
                request_key="reused-key",
                payload={"charge_id": "synthetic-charge",
                         "amount_minor": 999999},
            ))

        self.assertEqual(self.db.count(
            "safeact_operations", request_key="reused-key"
        ), 1)


if __name__ == "__main__":
    unittest.main()
