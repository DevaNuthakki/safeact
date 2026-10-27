"""Proposals and version-bound human approval (M3, D4: F14, A7 API side).

Operations are driven into the states a review needs through the real
worker and recovery roles; proposals and decisions go through the API.
Requires SAFEACT_RUN_DB_INTEGRATION=1 and SAFEACT_RUN_DISPOSABLE_DB_TESTS=1.
"""

import os
import secrets
import threading
import unittest
import uuid

from datetime import timedelta

import psycopg
from psycopg import sql

from api.cookies import CSRF_HEADER, SESSION_COOKIE
from connectors.base import LookupResult
from core.attempts import AttemptOutcome
from core.connector_profiles import crm_profile
from core.contracts import EvidenceKind
from core.lifecycle import RETRY_PENDING_REASON, LifecycleState as S
from storage import durable_lifecycle as lifecycle
from storage import durable_lifecycle_sql as lsql
from storage import product_api as product
from storage.experimental_recovery_worker import (
    ReconcileStatus, Reconciler, account_operations,
)
from storage.identity_admin import create_principal
from storage.lifecycle_environment import contract_insert, contract_params
from tests.integration.api_fixture import ApiEnvironment


ENABLED = (
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1"
)

REQUESTER = "demo-north-requester"
INVESTIGATOR = "demo-north-investigator"
REVIEWER = "demo-north-reviewer"
REVIEWER2 = "demo-north-reviewer2"
SOUTH_REVIEWER = "demo-south-reviewer"
LEAD = "demo-north-lead"
LEAD_PASSWORD = "lead-password-0001"


class StubLookup:
    """A connector that only answers lookups, with a fixed answer."""

    def __init__(self, connector_id, kind=EvidenceKind.NOT_FOUND):
        self.connector_id = connector_id
        self.kind = kind
        self.calls = 0

    def lookup(self, target, profile):
        self.calls += 1
        return LookupResult(self.kind)


@unittest.skipUnless(ENABLED, "PostgreSQL integration tests are opt-in")
class ReviewCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.api = ApiEnvironment().start()
        cls.addClassCleanup(cls.api.cleanup)
        # A lead holds every role: used to prove self-review is refused.
        with cls.api.admin.transaction():
            create_principal(
                cls.api.admin, cls.api.schema, tenant_id="tenant-north",
                username=LEAD, display_name="North Lead",
                roles=("requester", "investigator", "reviewer"),
                password=LEAD_PASSWORD,
                grants=(("mock-payments", "mock-payments-account",
                         "refund_payment", "ch_north_1004", "v1"),),
            )
        cls.api.passwords[LEAD] = LEAD_PASSWORD
        cls.clients = {
            name: cls.api.signed_in(name)
            for name in (REQUESTER, INVESTIGATOR, REVIEWER, REVIEWER2,
                         SOUTH_REVIEWER, LEAD)
        }
        cls.worker = cls.api.connect_role(cls.api.env.worker)
        cls.recoverer = cls.api.connect_role(cls.api.env.recoverer)
        cls.addClassCleanup(cls.worker.close)
        cls.addClassCleanup(cls.recoverer.close)

    # -- driving ------------------------------------------------------

    def post(self, who, path, body=None):
        client, csrf = self.clients[who]
        return client.post(path, json=body, headers={CSRF_HEADER: csrf})

    def get(self, who, path, **params):
        return self.clients[who][0].get(path, params=params)

    def submit(self, who=REQUESTER, body=None):
        body = body or {
            "request_key": "rk-" + uuid.uuid4().hex[:16],
            "action_type": "refund_payment",
            "provider_account_id": "mock-payments-account",
            "payload": {"charge_id": "ch_north_1001", "amount_minor": 1200},
        }
        response = self.post(who, "/actions", body)
        self.assertIn(response.status_code, (200, 201), response.text)
        return uuid.UUID(response.json()["operation_id"])

    def commit(self, connection, action):
        try:
            result = action()
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise

    def dispatch(self, operation_id):
        held = self.commit(self.worker, lambda: lifecycle.claim(
            self.worker, self.api.schema, operation_id, lease_seconds=300,
        ))
        return self.commit(self.worker, lambda: lifecycle.begin_dispatch(
            self.worker, self.api.schema, held, lease_seconds=300,
        ))

    def recover(self, operation_id, seq, expected, target, reason="test"):
        return self.commit(self.recoverer, lambda: lifecycle.recovery_transition(
            self.recoverer, self.api.schema, operation_id,
            expected_seq=seq, expected_state=expected, to_state=target,
            reason=reason,
        ))

    def escalated(self, who=REQUESTER, body=None):
        """An ambiguous dispatch, investigated and escalated."""

        operation_id = self.submit(who, body)
        fence = self.dispatch(operation_id)
        seq = self.commit(self.worker, lambda: lifecycle.record_dispatch_outcome(
            self.worker, self.api.schema, fence, S.UNKNOWN,
            reason="ambiguous_no_response",
        ))
        seq = self.recover(operation_id, seq, S.UNKNOWN, S.VERIFYING)
        self.recover(operation_id, seq, S.VERIFYING, S.ESCALATED_UNRESOLVED)
        return operation_id, fence.attempt_id

    def verifying_with_proof(self, case="case_north_2001"):
        """A CRM order rejected before execution: retry is protected."""

        operation_id = self.submit(body={
            "request_key": "rk-" + uuid.uuid4().hex[:16],
            "action_type": "create_replacement_order",
            "provider_account_id": "mock-crm-account",
            "payload": {"case_id": case, "sku": "sku-1"},
        })
        fence = self.dispatch(operation_id)
        self.commit(self.worker, lambda: lifecycle.record_attempt_outcome(
            self.worker, self.api.schema, fence.attempt_id,
            AttemptOutcome.PROVEN_NOT_APPLIED,
            evidence_kind=EvidenceKind.PROVEN_NOT_APPLIED,
            reference="crm-rejection-" + uuid.uuid4().hex[:8],
        ))
        seq = self.commit(self.worker, lambda: lifecycle.record_dispatch_outcome(
            self.worker, self.api.schema, fence, S.UNKNOWN,
            reason=RETRY_PENDING_REASON,
        ))
        self.recover(operation_id, seq, S.UNKNOWN, S.VERIFYING)
        return operation_id

    def propose(self, operation_id, action, who=INVESTIGATOR, reference=None):
        body = {"action": action, "rationale": "evidence reviewed"}
        if reference is not None:
            body["attested_reference"] = reference
        return self.post(who, f"/actions/{operation_id}/proposals", body)

    def proposed(self, operation_id, action, **kwargs):
        response = self.propose(operation_id, action, **kwargs)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def decide(self, proposal, who=REVIEWER, decision="approve",
               digest=None, key=None):
        return self.post(who, f"/proposals/{proposal['proposal_id']}/decision", {
            "decision": decision,
            "binding_digest": digest or proposal["binding_digest"],
            "decision_key": str(key or uuid.uuid4()),
        })

    def state(self, operation_id):
        return self.api.admin.execute(sql.SQL(
            "SELECT state, seq FROM {} WHERE operation_id = %s"
        ).format(self.api.table(lsql.CURRENT_STATE_VIEW)),
            (operation_id,)).fetchone()

    def audit(self, operation_id, event):
        return [row[0] for row in self.api.rows(sql.SQL(
            "SELECT details FROM {} WHERE operation_id = %s "
            "AND event_type = %s ORDER BY audit_id"
        ).format(self.api.table("safeact_audit_events")),
            (operation_id, event))]


class ProposalTests(ReviewCase):

    def test_a_proposal_snapshots_its_binding(self):
        operation_id, _ = self.escalated()
        proposal = self.proposed(operation_id, "REVERIFY")
        self.assertEqual(proposal["status"], "open")
        self.assertEqual(proposal["binding"]["state"], "ESCALATED_UNRESOLVED")
        self.assertEqual(proposal["binding"]["seq"], self.state(operation_id)[1])
        self.assertRegex(proposal["binding_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            proposal["binding"]["evidence_digest"],
            proposal["current"]["evidence_digest"],
        )
        self.assertIsNone(proposal["decision"])
        listed = self.get(REVIEWER, "/proposals").json()["items"]
        self.assertIn(proposal["proposal_id"], {p["proposal_id"] for p in listed})
        self.assertEqual(len(self.audit(operation_id, "PROPOSAL_RECORDED")), 1)

    def test_only_permitted_next_actions_can_be_proposed(self):
        recorded = self.submit()
        escalated, _ = self.escalated()
        payments_verifying = self.submit()
        fence = self.dispatch(payments_verifying)
        seq = self.commit(self.worker, lambda: lifecycle.record_dispatch_outcome(
            self.worker, self.api.schema, fence, S.UNKNOWN,
            reason="ambiguous_no_response",
        ))
        self.recover(payments_verifying, seq, S.UNKNOWN, S.VERIFYING)

        cases = (
            (recorded, "REVERIFY", None),
            (recorded, "ATTEST_APPLIED", "ref-1"),
            (escalated, "PROTECTED_RETRY", None),
            # Payments v1 has no review policy: nothing to approve.
            (payments_verifying, "PROTECTED_RETRY", None),
            (escalated, "ATTEST_APPLIED", " padded "),
            (escalated, "REVERIFY", "a-reference"),
        )
        for operation_id, action, reference in cases:
            with self.subTest(action=action, reference=reference):
                response = self.propose(operation_id, action, reference=reference)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["error"]["code"], "not_eligible")

        self.proposed(escalated, "REVERIFY")
        duplicate = self.propose(escalated, "REVERIFY")
        self.assertEqual(duplicate.status_code, 409)

    def test_only_investigators_propose_within_their_tenant(self):
        operation_id, _ = self.escalated()
        for who in (REQUESTER, REVIEWER):
            with self.subTest(who=who):
                self.assertEqual(
                    self.propose(operation_id, "REVERIFY", who=who).status_code,
                    403,
                )
        south = self.api.signed_in("demo-south-investigator")
        response = south[0].post(
            f"/actions/{operation_id}/proposals",
            json={"action": "REVERIFY", "rationale": "x"},
            headers={CSRF_HEADER: south[1]},
        )
        self.assertEqual(response.status_code, 404)


class DecisionTests(ReviewCase):

    def test_approved_reverify_grants_exactly_one_extra_lookup(self):
        operation_id, _ = self.escalated()
        proposal = self.proposed(operation_id, "REVERIFY")
        response = self.decide(proposal)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["outcome"], "approved")
        self.assertEqual(response.json()["proposal"]["status"], "approved")

        taken = self.commit(self.recoverer, lambda: lifecycle.take_reverify(
            self.recoverer, self.api.schema, operation_id
        ))
        self.assertEqual(str(taken), proposal["proposal_id"])
        again = self.commit(self.recoverer, lambda: lifecycle.take_reverify(
            self.recoverer, self.api.schema, operation_id
        ))
        self.assertIsNone(again)

    def test_reverify_lookup_runs_beyond_the_contract_bounds(self):
        operation_id, _ = self.escalated()
        stub = StubLookup("mock-payments")
        reconciler = Reconciler(
            connect=lambda: self.api.connect_role(self.api.env.recoverer),
            schema=self.api.schema, connectors=(stub,),
        )
        reconciler.reconcile_once()
        # The contract's check interval has not elapsed: no second look.
        reconciler.reconcile_once()
        baseline = stub.calls

        proposal = self.proposed(operation_id, "REVERIFY")
        self.decide(proposal)
        reconciler.reconcile_once()
        self.assertEqual(stub.calls, baseline + 1)
        reconciler.reconcile_once()
        self.assertEqual(stub.calls, baseline + 1)
        # An empty lookup reopens nothing.
        self.assertEqual(self.state(operation_id)[0], "ESCALATED_UNRESOLVED")

    def test_attestation_reaches_success_with_human_provenance(self):
        operation_id, attempt_id = self.escalated()
        proposal = self.proposed(
            operation_id, "ATTEST_APPLIED", reference="re_dashboard_8842"
        )
        self.assertEqual(self.decide(proposal).json()["outcome"], "approved")
        rows = self.api.rows(sql.SQL(
            "SELECT outcome, evidence_kind, evidence_reference, provenance, "
            "observed_by FROM {} WHERE operation_id = %s "
            "AND provenance = 'human_attested'"
        ).format(self.api.table("safeact_attempt_outcomes")), (operation_id,))
        self.assertEqual(rows, [(
            "confirmed_applied", "applied", "re_dashboard_8842",
            "human_attested", "reviewer:" + REVIEWER,
        )])

        reconciler = Reconciler(
            connect=lambda: self.api.connect_role(self.api.env.recoverer),
            schema=self.api.schema,
            connectors=(StubLookup("mock-payments"),),
        )
        reconciler.reconcile_once()
        self.assertEqual(self.state(operation_id)[0], "SUCCEEDED")

        detail = self.get(REQUESTER, f"/actions/{operation_id}").json()
        self.assertEqual(detail["success_provenance"], "human_attested")
        provenances = {
            o["provenance"] for a in detail["attempts"] for o in a["outcomes"]
        }
        self.assertIn("human_attested", provenances)
        accounting = account_operations(self.recoverer, self.api.schema)
        self.assertGreaterEqual(accounting.succeeded_human_attested, 1)
        self.assertEqual(
            accounting.succeeded_human_attested
            + accounting.succeeded_provider_verified,
            accounting.by_state[S.SUCCEEDED],
        )

    def test_protected_retry_needs_the_approval_and_the_approval_moves_it(self):
        operation_id = self.verifying_with_proof()
        state, seq = self.state(operation_id)
        self.assertEqual(state, "VERIFYING")

        # Without approval the database refuses the retry outright.
        with self.assertRaises(lifecycle.ApprovalRequired):
            self.recover(operation_id, seq, S.VERIFYING, S.RETRY_ELIGIBLE)
        self.assertEqual(
            self.commit(self.recoverer, lambda: lifecycle.retry_approval_status(
                self.recoverer, self.api.schema, operation_id
            )), "awaiting",
        )

        # The reconciler waits for the reviewer instead of looking again.
        stub = StubLookup("mock-crm")
        result = Reconciler(
            connect=lambda: self.api.connect_role(self.api.env.recoverer),
            schema=self.api.schema, connectors=(stub,), retry_authorized=True,
        ).reconcile(operation_id, self.recoverer)
        self.assertEqual(result.status, ReconcileStatus.WAITING)
        self.assertEqual(stub.calls, 0)
        self.assertEqual(self.state(operation_id), (state, seq))

        proposal = self.proposed(operation_id, "PROTECTED_RETRY")
        response = self.decide(proposal)
        self.assertEqual(response.json()["outcome"], "approved")
        self.assertEqual(self.state(operation_id), ("RETRY_ELIGIBLE", seq + 1))
        transition = self.api.rows(sql.SQL(
            "SELECT reason, evidence FROM {} WHERE operation_id = %s "
            "AND seq = %s"
        ).format(self.api.table("safeact_operation_transitions")),
            (operation_id, seq + 1))[0]
        self.assertEqual(transition[0], "retry_approved_by_reviewer")
        self.assertEqual(transition[1]["proposal_id"], proposal["proposal_id"])

        # The retry is reclaimed by a worker under every earlier guard.
        held = self.commit(self.worker, lambda: lifecycle.claim(
            self.worker, self.api.schema, operation_id, lease_seconds=60,
        ))
        self.assertEqual(held.seq, seq + 2)

    def test_after_the_deadline_an_unapproved_retry_is_decided_without_one(self):
        quick = crm_profile("v1-quick", investigation_window=timedelta(0))
        with self.api.admin.transaction():
            self.api.admin.execute(
                contract_insert(self.api.schema), contract_params(quick)
            )
            self.api.admin.execute(sql.SQL(
                "INSERT INTO {}.safeact_contract_review_policies "
                "VALUES ('mock-crm', 'v1-quick', true)"
            ).format(sql.Identifier(self.api.schema)))
            self.api.admin.execute(sql.SQL(
                "INSERT INTO {} (principal_id, connector_id, provider_account_id,"
                " action_type, resource_id, contract_version) VALUES "
                "(%s, 'mock-crm', 'mock-crm-account', 'create_replacement_order',"
                " 'case_north_2003', 'v1-quick')"
            ).format(self.api.table("safeact_action_grants")),
                (self.api.principal_id(REQUESTER),))
        operation_id = self.submit(body={
            "request_key": "rk-" + uuid.uuid4().hex[:16],
            "action_type": "create_replacement_order",
            "provider_account_id": "mock-crm-account",
            "contract_version": "v1-quick",
            "payload": {"case_id": "case_north_2003", "sku": "sku-1"},
        })
        fence = self.dispatch(operation_id)
        self.commit(self.worker, lambda: lifecycle.record_attempt_outcome(
            self.worker, self.api.schema, fence.attempt_id,
            AttemptOutcome.PROVEN_NOT_APPLIED,
            evidence_kind=EvidenceKind.PROVEN_NOT_APPLIED, reference="rej-1",
        ))
        seq = self.commit(self.worker, lambda: lifecycle.record_dispatch_outcome(
            self.worker, self.api.schema, fence, S.UNKNOWN,
            reason=RETRY_PENDING_REASON,
        ))
        self.recover(operation_id, seq, S.UNKNOWN, S.VERIFYING)

        # Too late to propose; and the reconciler stops waiting.
        self.assertEqual(
            self.propose(operation_id, "PROTECTED_RETRY").status_code, 409
        )
        Reconciler(
            connect=lambda: self.api.connect_role(self.api.env.recoverer),
            schema=self.api.schema, connectors=(StubLookup("mock-crm"),),
            retry_authorized=True,
        ).reconcile(operation_id, self.recoverer)
        self.assertEqual(self.state(operation_id)[0], "FAILED_CONFIRMED")
        detail = self.get(REQUESTER, f"/actions/{operation_id}").json()
        self.assertEqual(
            detail["transitions"][-1]["reason"], "retry_approval_expired"
        )
        accounting = account_operations(self.recoverer, self.api.schema)
        self.assertEqual(accounting.failed_retry_approval_expired, 1)

    def test_reject_changes_nothing_but_is_recorded(self):
        operation_id, _ = self.escalated()
        before = self.state(operation_id)
        proposal = self.proposed(
            operation_id, "ATTEST_APPLIED", reference="re_unverified_1"
        )
        response = self.decide(proposal, decision="reject")
        self.assertEqual(response.json()["outcome"], "rejected")
        self.assertEqual(self.state(operation_id), before)
        self.assertEqual(self.api.count(
            "safeact_attempt_outcomes",
            sql.SQL("operation_id = %s AND provenance = 'human_attested'"),
            (operation_id,),
        ), 0)
        self.assertEqual(
            self.audit(operation_id, "DECISION_RECORDED")[0]["decision"],
            "reject",
        )


class StaleAndConcurrentTests(ReviewCase):
    """F14: one valid, version-bound decision."""

    def assert_stale(self, response, reason):
        self.assertEqual(response.status_code, 409, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], "stale_approval")
        self.assertEqual(error["reason"], reason)
        self.assertIsNone(error["proposal"]["decision"])

    def test_state_change_between_view_and_decision_is_stale(self):
        operation_id = self.verifying_with_proof("case_north_2002")
        proposal = self.proposed(operation_id, "PROTECTED_RETRY")
        _, seq = self.state(operation_id)
        self.recover(operation_id, seq, S.VERIFYING, S.UNKNOWN,
                     reason="verify_later")

        self.assert_stale(self.decide(proposal), "operation_changed")
        self.assertEqual(self.state(operation_id), ("UNKNOWN", seq + 1))
        refusals = self.audit(operation_id, "APPROVAL_REFUSED_STALE")
        self.assertEqual(refusals[0]["reason"], "operation_changed")
        self.assertEqual(
            self.get(REVIEWER, f"/proposals/{proposal['proposal_id']}")
            .json()["status"], "stale",
        )

    def test_new_evidence_makes_a_proposal_stale(self):
        operation_id, attempt_id = self.escalated()
        proposal = self.proposed(operation_id, "REVERIFY")
        self.commit(self.recoverer, lambda: lifecycle.record_attempt_outcome(
            self.recoverer, self.api.schema, attempt_id,
            AttemptOutcome.MAY_HAVE_DISPATCHED,
            evidence_kind=EvidenceKind.NOT_FOUND,
        ))
        self.assert_stale(self.decide(proposal), "evidence_changed")

    def test_expired_proposal_is_stale(self):
        operation_id, _ = self.escalated()
        expires = self.api.admin.execute(
            "SELECT date_trunc('milliseconds', now() - interval '1 minute')"
        ).fetchone()[0]
        binding = self.api.admin.execute(sql.SQL(
            "SELECT * FROM {}.safeact_proposal_digest(%s, 'REVERIFY', NULL, %s)"
        ).format(sql.Identifier(self.api.schema)), (operation_id, expires)
        ).fetchone()
        proposal_id = uuid.uuid4()
        self.api.admin.execute(sql.SQL(
            "INSERT INTO {} (proposal_id, operation_id, tenant_id, action, "
            "rationale, proposed_by, bound_seq, bound_state, "
            "bound_payload_sha256, bound_evidence_digest, bound_connector_id, "
            "bound_contract_version, binding_digest, expires_at, created_at) "
            "VALUES (%s, %s, 'tenant-north', 'REVERIFY', 'old', %s, %s, %s, "
            "%s, %s, %s, %s, %s, %s, %s - interval '1 minute')"
        ).format(self.api.table("safeact_proposals")), (
            proposal_id, operation_id, self.api.principal_id(INVESTIGATOR),
            *binding[:6], binding[6], expires, expires,
        ))
        response = self.decide({
            "proposal_id": str(proposal_id), "binding_digest": binding[6],
        })
        self.assert_stale(response, "expired")

    def test_a_screen_showing_an_old_binding_is_stale(self):
        operation_id, _ = self.escalated()
        proposal = self.proposed(operation_id, "REVERIFY")
        self.assert_stale(
            self.decide(proposal, digest=secrets.token_hex(32)),
            "client_out_of_date",
        )
        self.assertEqual(self.decide(proposal).json()["outcome"], "approved")

    def test_two_reviewers_at_once_make_one_decision(self):
        for _ in range(6):
            operation_id, _ = self.escalated()
            proposal = self.proposed(
                operation_id, "ATTEST_APPLIED", reference="re_" + uuid.uuid4().hex[:8]
            )
            barrier = threading.Barrier(2)
            results = {}

            def review(who):
                barrier.wait()
                results[who] = self.decide(proposal, who=who)

            threads = [threading.Thread(target=review, args=(who,))
                       for who in (REVIEWER, REVIEWER2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            codes = sorted(r.status_code for r in results.values())
            self.assertEqual(codes, [200, 409])
            loser = next(r for r in results.values() if r.status_code == 409)
            self.assertEqual(loser.json()["error"]["code"], "already_decided")
            self.assertEqual(self.api.count(
                "safeact_decisions", sql.SQL("proposal_id = %s"),
                (proposal["proposal_id"],),
            ), 1)
            self.assertEqual(self.api.count(
                "safeact_attempt_outcomes",
                sql.SQL("operation_id = %s AND provenance = 'human_attested'"),
                (operation_id,),
            ), 1)

    def test_a_repeated_click_returns_the_same_decision(self):
        operation_id, _ = self.escalated()
        proposal = self.proposed(
            operation_id, "ATTEST_APPLIED", reference="re_click_1"
        )
        key = uuid.uuid4()
        first = self.decide(proposal, key=key)
        second = self.decide(proposal, key=key)
        self.assertEqual(first.json()["outcome"], "approved")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["outcome"], "replayed")
        self.assertEqual(self.api.count(
            "safeact_attempt_outcomes",
            sql.SQL("operation_id = %s AND provenance = 'human_attested'"),
            (operation_id,),
        ), 1)
        other_key = self.decide(proposal, key=uuid.uuid4())
        self.assertEqual(other_key.json()["error"]["code"], "already_decided")


class HumanOnlyApprovalTests(ReviewCase):
    """A7 at the API: tenant isolation and human-only, independent approval."""

    def test_no_one_decides_on_their_own_proposal_for_any_action(self):
        escalated, _ = self.escalated()
        verifying = self.verifying_with_proof("case_north_2003")
        for operation_id, action, reference in (
            (escalated, "REVERIFY", None),
            (escalated, "ATTEST_APPLIED", "re_self_1"),
            (verifying, "PROTECTED_RETRY", None),
        ):
            with self.subTest(action=action):
                proposal = self.proposed(
                    operation_id, action, who=LEAD, reference=reference
                )
                response = self.decide(proposal, who=LEAD)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error"]["code"], "self_review")
                self.assertEqual(self.decide(proposal).status_code, 200)

    def test_no_one_decides_on_an_action_they_requested(self):
        operation_id, _ = self.escalated(who=LEAD, body={
            "request_key": "rk-" + uuid.uuid4().hex[:16],
            "action_type": "refund_payment",
            "provider_account_id": "mock-payments-account",
            "payload": {"charge_id": "ch_north_1004", "amount_minor": 50},
        })
        for action, reference in (("REVERIFY", None),
                                  ("ATTEST_APPLIED", "re_own_1")):
            with self.subTest(action=action):
                proposal = self.proposed(operation_id, action, reference=reference)
                response = self.decide(proposal, who=LEAD)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error"]["code"], "self_review")

    def test_only_reviewers_decide(self):
        operation_id, _ = self.escalated()
        proposal = self.proposed(operation_id, "REVERIFY")
        for who in (INVESTIGATOR, REQUESTER):
            with self.subTest(who=who):
                self.assertEqual(self.decide(proposal, who=who).status_code, 403)
        anonymous = self.api.client().post(
            f"/proposals/{proposal['proposal_id']}/decision",
            json={"decision": "approve", "binding_digest": proposal["binding_digest"],
                  "decision_key": str(uuid.uuid4())},
        )
        self.assertEqual(anonymous.status_code, 401)

    def test_other_tenants_cannot_see_or_decide(self):
        operation_id, _ = self.escalated()
        proposal = self.proposed(operation_id, "REVERIFY")
        south = self.decide(proposal, who=SOUTH_REVIEWER)
        self.assertEqual(south.status_code, 404)
        self.assertEqual(
            self.get(SOUTH_REVIEWER, f"/proposals/{proposal['proposal_id']}").status_code,
            404,
        )
        listed = self.get(SOUTH_REVIEWER, "/proposals", status="all").json()["items"]
        self.assertNotIn(proposal["proposal_id"], {p["proposal_id"] for p in listed})

    def test_the_api_role_cannot_decide_without_a_reviewer_session(self):
        operation_id, _ = self.escalated()
        proposal = self.proposed(operation_id, "REVERIFY")
        _, reviewer_csrf = self.clients[REVIEWER]
        for forged in (None, secrets.token_hex(32)):
            with self.subTest(secret=forged):
                with self.api.connect_api() as api:
                    with self.assertRaises(product.Unauthenticated):
                        product.decide(
                            api, self.api.schema, secret=forged,
                            csrf=reviewer_csrf,
                            proposal_id=proposal["proposal_id"],
                            decision="approve",
                            binding_digest=proposal["binding_digest"],
                            decision_key=uuid.uuid4(),
                        )
        # Even with the investigator's real session it is not a review.
        investigator, csrf = self.clients[INVESTIGATOR]
        with self.api.connect_api() as api:
            with self.assertRaises(product.RoleRequired):
                product.decide(
                    api, self.api.schema,
                    secret=investigator.cookies.get(SESSION_COOKIE), csrf=csrf,
                    proposal_id=proposal["proposal_id"], decision="approve",
                    binding_digest=proposal["binding_digest"],
                    decision_key=uuid.uuid4(),
                )


class ProvenanceGuardTests(ReviewCase):
    """A human can attest an applied effect, never non-application."""

    def test_human_provenance_is_only_ever_an_applied_effect(self):
        operation_id, attempt_id = self.escalated()
        for outcome, kind, reference in (
            ("proven_not_applied", "proven_not_applied", "r1"),
            ("may_have_dispatched", "not_found", None),
            ("may_have_dispatched", "inconclusive", None),
            ("confirmed_applied", "applied", None),
        ):
            with self.subTest(outcome=outcome, kind=kind):
                with self.assertRaises(psycopg.errors.CheckViolation):
                    self.api.admin.execute(sql.SQL(
                        "INSERT INTO {} (outcome_id, operation_id, attempt_id, "
                        "outcome, evidence_kind, evidence_reference, observed_by, "
                        "observed_seq, provenance) VALUES (gen_random_uuid(), "
                        "%s, %s, %s, %s, %s, 'x', 0, 'human_attested')"
                    ).format(self.api.table("safeact_attempt_outcomes")),
                        (operation_id, attempt_id, outcome, kind, reference))

    def test_an_attestation_never_makes_failure_or_retry_legal(self):
        operation_id, _ = self.escalated()
        proposal = self.proposed(operation_id, "ATTEST_APPLIED", reference="re_x_9")
        self.decide(proposal)
        _, seq = self.state(operation_id)
        seq = self.recover(
            operation_id, seq, S.ESCALATED_UNRESOLVED, S.VERIFYING,
            reason="reopen_recorded_evidence",
        )
        with self.assertRaises(lifecycle.EvidenceRequired):
            self.recover(operation_id, seq, S.VERIFYING, S.FAILED_CONFIRMED)
        with self.assertRaises(lifecycle.RetryNotProtected):
            self.recover(operation_id, seq, S.VERIFYING, S.RETRY_ELIGIBLE)


if __name__ == "__main__":
    unittest.main()
