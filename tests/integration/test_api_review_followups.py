"""Review follow-ups (0015): direct verification, expired approval, policy binding.

Requires SAFEACT_RUN_DB_INTEGRATION=1 and SAFEACT_RUN_DISPOSABLE_DB_TESTS=1.
"""

import unittest
import uuid

from datetime import timedelta

import psycopg
from psycopg import sql

from connectors.base import LookupResult
from core.attempts import AttemptOutcome
from core.connector_profiles import crm_profile, payments_profile
from core.contracts import EvidenceKind
from core.lifecycle import LifecycleState as S
from storage import durable_lifecycle as lifecycle
from storage import durable_lifecycle_sql as lsql
from storage.experimental_recovery_worker import Reconciler
from storage.identity_admin import APPLICATION_IDLE_TIMEOUT
from storage.lifecycle_environment import contract_insert, contract_params
from tests.integration.test_api_review import (
    INVESTIGATOR, REQUESTER, REVIEWER, ReviewCase, StubLookup,
)


class Answer(StubLookup):
    """A stub lookup whose answer can be changed between passes."""

    def __init__(self, connector_id):
        super().__init__(connector_id)
        self.result = LookupResult(EvidenceKind.NOT_FOUND)

    def lookup(self, target, profile):
        self.calls += 1
        return self.result


class FollowupCase(ReviewCase):

    def reconciler(self, *connectors):
        return Reconciler(
            connect=lambda: self.api.connect_role(self.api.env.recoverer),
            schema=self.api.schema, connectors=connectors,
        )

    def request(self, operation_id, who=INVESTIGATOR):
        return self.post(who, f"/actions/{operation_id}/verification-requests")

    def register(self, profile, *, policy=False, grant=None):
        with self.api.admin.transaction():
            self.api.admin.execute(
                contract_insert(self.api.schema), contract_params(profile)
            )
            if policy:
                self.api.admin.execute(sql.SQL(
                    "INSERT INTO {}.safeact_contract_review_policies "
                    "VALUES (%s, %s, true)"
                ).format(sql.Identifier(self.api.schema)),
                    (profile.connector_id, profile.version))
            if grant:
                self.api.admin.execute(sql.SQL(
                    "INSERT INTO {} (principal_id, connector_id, "
                    "provider_account_id, action_type, resource_id, "
                    "contract_version) VALUES (%s, %s, %s, %s, %s, %s)"
                ).format(self.api.table("safeact_action_grants")),
                    (self.api.principal_id(REQUESTER), *grant, profile.version))


class DirectVerificationTests(FollowupCase):

    def test_an_investigator_queues_a_bounded_lookup_without_approval(self):
        operation_id, _ = self.escalated()
        response = self.request(operation_id)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "queued")

        stub = Answer("mock-payments")
        self.reconciler(stub).reconcile(operation_id, self.recoverer)
        self.assertEqual(stub.calls, 1)
        self.assertEqual(self.api.count(
            "safeact_verification_request_uses",
            sql.SQL("request_id = %s"), (response.json()["request_id"],),
        ), 1)
        sources = {row[0]["pass"] for row in self.api.rows(sql.SQL(
            "SELECT details FROM {} WHERE operation_id = %s "
            "AND details ? 'pass'"
        ).format(self.api.table("safeact_attempt_outcomes")), (operation_id,))}
        self.assertIn("requested_verification", sources)
        # An empty answer reopens nothing.
        self.assertEqual(self.state(operation_id)[0], "ESCALATED_UNRESOLVED")

    def test_the_contract_interval_holds_for_requests(self):
        operation_id, _ = self.escalated()
        self.reconciler(Answer("mock-payments")).reconcile(
            operation_id, self.recoverer
        )
        # A lookup just ran: the next one waits for the check interval.
        response = self.request(operation_id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "not_eligible")

    def test_the_bounds_are_checked_again_when_the_lookup_would_run(self):
        operation_id, attempt_id = self.escalated()
        self.assertEqual(self.request(operation_id).status_code, 201)
        # A lookup lands after the request (here recorded directly).
        self.commit(self.recoverer, lambda: lifecycle.record_attempt_outcome(
            self.recoverer, self.api.schema, attempt_id,
            AttemptOutcome.MAY_HAVE_DISPATCHED,
            evidence_kind=EvidenceKind.NOT_FOUND,
        ))
        taken = self.commit(
            self.recoverer, lambda: lifecycle.take_verification_request(
                self.recoverer, self.api.schema, operation_id
            )
        )
        self.assertIsNone(taken)

    def test_at_most_three_requests_per_escalation(self):
        operation_id, _ = self.escalated()
        codes = [self.request(operation_id).status_code for _ in range(4)]
        self.assertEqual(codes, [201, 201, 201, 409])

    def test_beyond_the_horizon_only_a_reviewed_reverify_helps(self):
        short = payments_profile("v1-short", horizon=timedelta(0))
        self.register(short, grant=(
            "mock-payments", "mock-payments-account", "refund_payment",
            "ch_north_1003",
        ))
        operation_id, _ = self.escalated(body={
            "request_key": "rk-" + uuid.uuid4().hex[:16],
            "action_type": "refund_payment",
            "provider_account_id": "mock-payments-account",
            "contract_version": "v1-short",
            "payload": {"charge_id": "ch_north_1003", "amount_minor": 10},
        })
        refused = self.request(operation_id)
        self.assertEqual(refused.status_code, 409)

        proposal = self.proposed(operation_id, "REVERIFY")
        self.decide(proposal)
        stub = Answer("mock-payments")
        self.reconciler(stub).reconcile(operation_id, self.recoverer)
        self.assertEqual(stub.calls, 1)

    def test_a_decisive_answer_reopens_and_resolves(self):
        operation_id, _ = self.escalated()
        self.request(operation_id)
        stub = Answer("mock-payments")
        stub.result = LookupResult(EvidenceKind.APPLIED, reference="re_found_77")
        reconciler = self.reconciler(stub)
        reconciler.reconcile(operation_id, self.recoverer)
        self.assertEqual(self.state(operation_id)[0], "SUCCEEDED")
        detail = self.get(REQUESTER, f"/actions/{operation_id}").json()
        self.assertEqual(detail["success_provenance"], "provider_verified")

    def test_only_investigators_and_only_escalated_work(self):
        escalated, _ = self.escalated()
        for who in (REQUESTER, REVIEWER):
            with self.subTest(who=who):
                self.assertEqual(self.request(escalated, who=who).status_code, 403)
        recorded = self.submit()
        self.assertEqual(self.request(recorded).status_code, 409)
        self.assertEqual(self.request(uuid.uuid4()).status_code, 404)
        south = self.api.signed_in("demo-south-investigator")
        response = south[0].post(
            f"/actions/{escalated}/verification-requests",
            headers={"X-SafeAct-CSRF": south[1]},
        )
        self.assertEqual(response.status_code, 404)


class ApprovalExpiryTests(FollowupCase):

    def test_the_expired_reason_is_refused_outside_its_case(self):
        # A payments operation (no review policy) with proof: the reason
        # cannot be borrowed for an ordinary confirmed failure.
        operation_id = self.submit()
        fence = self.dispatch(operation_id)
        self.commit(self.worker, lambda: lifecycle.record_attempt_outcome(
            self.worker, self.api.schema, fence.attempt_id,
            AttemptOutcome.PROVEN_NOT_APPLIED,
            evidence_kind=EvidenceKind.PROVEN_NOT_APPLIED, reference="rej-9",
        ))
        seq = self.commit(self.worker, lambda: lifecycle.record_dispatch_outcome(
            self.worker, self.api.schema, fence, S.UNKNOWN,
            reason="rejected_before_execution_retry_pending",
        ))
        seq = self.recover(operation_id, seq, S.UNKNOWN, S.VERIFYING)
        with self.assertRaises(lifecycle.IllegalTransition):
            self.recover(operation_id, seq, S.VERIFYING, S.FAILED_CONFIRMED,
                         reason=lsql.APPROVAL_EXPIRED_REASON)
        # The ordinary confirmed failure is still available.
        self.recover(operation_id, seq, S.VERIFYING, S.FAILED_CONFIRMED,
                     reason="proven_not_applied")

    def test_not_before_the_deadline_on_a_reviewed_contract(self):
        operation_id = self.verifying_with_proof("case_north_2001")
        _, seq = self.state(operation_id)
        with self.assertRaises(lifecycle.IllegalTransition):
            self.recover(operation_id, seq, S.VERIFYING, S.FAILED_CONFIRMED,
                         reason=lsql.APPROVAL_EXPIRED_REASON)


class PolicyBindingTests(FollowupCase):

    def test_a_policy_row_appearing_makes_old_proposals_stale(self):
        profile = crm_profile("v1-unreviewed")
        self.register(profile, grant=(
            "mock-crm", "mock-crm-account", "create_replacement_order",
            "case_north_2003",
        ))
        operation_id, _ = self.escalated(body={
            "request_key": "rk-" + uuid.uuid4().hex[:16],
            "action_type": "create_replacement_order",
            "provider_account_id": "mock-crm-account",
            "contract_version": "v1-unreviewed",
            "payload": {"case_id": "case_north_2003", "sku": "sku-2"},
        })
        proposal = self.proposed(operation_id, "REVERIFY")
        self.api.admin.execute(sql.SQL(
            "INSERT INTO {}.safeact_contract_review_policies "
            "VALUES ('mock-crm', 'v1-unreviewed', true)"
        ).format(sql.Identifier(self.api.schema)))

        view = self.get(REVIEWER, f"/proposals/{proposal['proposal_id']}").json()
        self.assertEqual(view["status"], "stale")
        response = self.decide(proposal)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["reason"], "binding_changed")

    def test_policies_are_insert_only(self):
        for statement in (
            "UPDATE {}.safeact_contract_review_policies "
            "SET retry_requires_approval = false",
            "DELETE FROM {}.safeact_contract_review_policies",
        ):
            with self.subTest(statement=statement[:6]):
                with self.assertRaises(psycopg.Error):
                    self.api.admin.execute(sql.SQL(statement).format(
                        sql.Identifier(self.api.schema)
                    ))


class IdleTimeoutTests(FollowupCase):

    def test_every_application_role_ends_abandoned_transactions(self):
        rows = self.api.rows(
            "SELECT r.rolname, unnest(s.setconfig) FROM pg_db_role_setting s "
            "JOIN pg_roles r ON r.oid = s.setrole WHERE r.rolname = ANY(%s)",
            (list(self.api.env.login_roles),),
        )
        timeouts = {
            role: setting.split("=", 1)[1] for role, setting in rows
            if setting.startswith("idle_in_transaction_session_timeout=")
        }
        self.assertEqual(
            timeouts,
            {role: APPLICATION_IDLE_TIMEOUT for role in self.api.env.login_roles},
        )


if __name__ == "__main__":
    unittest.main()
