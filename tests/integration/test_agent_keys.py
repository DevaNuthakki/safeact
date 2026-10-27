"""Agent keys and the agent SQL boundary (M4.1, migration 0016).

Every call goes through the safeact_mcp_* functions as the MCP login
role, with identity, tenant and scope derived from the key inside
PostgreSQL. Covers key format, expiry, revocation, disablement, scopes,
own-only visibility, derived request keys and SAT28 (F15, "agent
changes key"), agent proposals, and the privilege split between the API
and MCP roles. Requires SAFEACT_RUN_DB_INTEGRATION=1 and
SAFEACT_RUN_DISPOSABLE_DB_TESTS=1.
"""

import itertools
import json
import os
import secrets
import threading
import time
import unittest
import uuid

from datetime import timedelta

import psycopg
from psycopg import sql

from api.cookies import CSRF_HEADER, SESSION_COOKIE
from core.lifecycle import LifecycleState as S
from storage import agent_api as agent
from storage import durable_lifecycle as lifecycle
from storage import durable_lifecycle_sql as lsql
from storage import product_api as product
from storage.identity_admin import (
    create_principal, issue_agent_key, revoke_agent_key,
)
from tests.integration.api_fixture import ApiEnvironment


ENABLED = (
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1"
)

NORTH_AGENT = "demo-north-agent"
SOUTH_AGENT = "demo-south-agent"
REQUESTER = "demo-north-requester"
INVESTIGATOR = "demo-north-investigator"
REVIEWER = "demo-north-reviewer"

PAYMENTS = ("mock-payments", "mock-payments-account", "v1")
# Extra synthetic charges granted to the north agent, one per test, so
# SAT28 (one unsettled operation per target) never couples tests.
DEFAULT = object()
POOL = tuple(f"ch_north_agent_{n:03d}" for n in range(1, 41))


def refund(charge, amount=1200):
    return json.dumps(
        {"amount_minor": amount, "charge_id": charge},
        separators=(",", ":"), sort_keys=True,
    )


@unittest.skipUnless(ENABLED, "PostgreSQL integration tests are opt-in")
class AgentKeyTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.api = ApiEnvironment().start()
        cls.addClassCleanup(cls.api.cleanup)
        cls.mcp = cls.api.connect_mcp()
        cls.addClassCleanup(cls.mcp.close)
        cls.keys = cls.api.agent_keys
        cls.worker = cls.api.connect_role(cls.api.env.worker)
        cls.recoverer = cls.api.connect_role(cls.api.env.recoverer)
        cls.addClassCleanup(cls.worker.close)
        cls.addClassCleanup(cls.recoverer.close)
        connector, account, version = PAYMENTS
        principal = cls.api.principal_id(NORTH_AGENT)
        with cls.api.admin.transaction():
            for charge in POOL:
                cls.api.admin.execute(sql.SQL(
                    "INSERT INTO {} (principal_id, connector_id, "
                    "provider_account_id, action_type, resource_id, "
                    "contract_version) VALUES (%s, %s, %s, %s, %s, %s)"
                ).format(cls.api.table("safeact_action_grants")), (
                    principal, connector, account, "refund_payment", charge,
                    version,
                ))
        cls.pool = itertools.chain(POOL)

    def fresh(self):
        return next(self.pool)

    # -- helpers ------------------------------------------------------

    def submit(self, charge, intent=DEFAULT, *, who=NORTH_AGENT,
               key=DEFAULT, amount=1200, connection=None):
        connector, account, version = PAYMENTS
        return agent.submit(
            connection or self.mcp, self.api.schema,
            key=self.keys[who] if key is DEFAULT else key,
            intent_ref=(
                "intent-" + uuid.uuid4().hex[:8] if intent is DEFAULT
                else intent
            ),
            action_type="refund_payment", resource_id=charge,
            provider_account_id=account, connector_id=connector,
            contract_version=version, payload=refund(charge, amount),
        )

    def view(self, operation_id, who=NORTH_AGENT, key=DEFAULT):
        return agent.operation(
            self.mcp, self.api.schema,
            key=self.keys[who] if key is DEFAULT else key,
            operation_id=operation_id,
        )

    def principal(self, username):
        return self.api.principal_id(username)

    def service_principal(self, username, roles=("investigator", "requester"),
                          grants=()):
        with self.api.admin.transaction():
            return create_principal(
                self.api.admin, self.api.schema, tenant_id="tenant-north",
                username=username, display_name=username, kind="service",
                roles=roles, grants=grants,
            )

    def key_for(self, principal_id, scopes=("investigate", "submit"),
                lifetime=timedelta(days=1)):
        with self.api.admin.transaction():
            return issue_agent_key(
                self.api.admin, self.api.schema, principal_id=principal_id,
                scopes=scopes, label="test", created_by="test",
                lifetime=lifetime,
            )

    def commit(self, connection, action):
        try:
            result = action()
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise

    def escalate(self, operation_id):
        held = self.commit(self.worker, lambda: lifecycle.claim(
            self.worker, self.api.schema, operation_id, lease_seconds=300,
        ))
        fence = self.commit(self.worker, lambda: lifecycle.begin_dispatch(
            self.worker, self.api.schema, held, lease_seconds=300,
        ))
        seq = self.commit(self.worker, lambda: lifecycle.record_dispatch_outcome(
            self.worker, self.api.schema, fence, S.UNKNOWN,
            reason="ambiguous_no_response",
        ))
        for current, target in ((S.UNKNOWN, S.VERIFYING),
                                (S.VERIFYING, S.ESCALATED_UNRESOLVED)):
            seq = self.commit(self.recoverer, lambda: lifecycle.recovery_transition(
                self.recoverer, self.api.schema, operation_id,
                expected_seq=seq, expected_state=current, to_state=target,
                reason="test",
            ))

    def count(self, table):
        return self.api.count(table)

    # -- admission ------------------------------------------------------

    def test_the_request_key_is_derived_and_the_same_intent_is_one_operation(self):
        charge = self.fresh()
        first = self.submit(charge, "refund-order-1")
        again = self.submit(charge, "refund-order-1")

        self.assertTrue(first.created)
        self.assertFalse(again.created)
        self.assertEqual(again.operation_id, first.operation_id)
        self.assertRegex(first.request_key, r"^ag:[0-9a-f]{64}$")
        actor = self.api.rows(sql.SQL(
            "SELECT actor_id FROM {} WHERE operation_id = %s "
            "AND event_type = 'INTENT_RECORDED'"
        ).format(self.api.table("safeact_audit_events")),
            (first.operation_id,))
        self.assertEqual(actor, [(str(self.principal(NORTH_AGENT)),)])
        self.assertEqual(
            self.view(first.operation_id)["operation_id"],
            str(first.operation_id),
        )

    def test_same_intent_with_a_changed_payload_is_a_conflict(self):
        charge = self.fresh()
        self.submit(charge, "refund-order-2", amount=500)
        with self.assertRaises(product.RequestConflict):
            self.submit(charge, "refund-order-2", amount=900)

    def test_a_new_intent_on_an_unsettled_target_is_refused(self):
        # F15, "agent changes key": a fresh intent reference cannot open
        # a second operation while the first is not settled.
        charge = self.fresh()
        first = self.submit(charge, "refund-order-3")
        before = self.count("safeact_operations")
        with self.assertRaises(product.ConflictingOperation):
            self.submit(charge, "refund-order-3-again")
        self.assertEqual(self.count("safeact_operations"), before)

        # Once the first is cancelled (settled), a new intent is admitted.
        client, csrf = self.api.signed_in(INVESTIGATOR)
        response = client.post(
            f"/actions/{first.operation_id}/cancel", headers={CSRF_HEADER: csrf}
        )
        self.assertEqual(response.status_code, 200, response.text)
        later = self.submit(charge, "refund-order-3-again")
        self.assertTrue(later.created)

    def test_concurrent_new_intents_on_one_target_admit_exactly_one(self):
        charge = self.fresh()
        results, barrier = [], threading.Barrier(4)

        def attempt(n):
            with self.api.connect_mcp() as connection:
                barrier.wait()
                try:
                    results.append(self.submit(
                        charge, f"race-{n}", connection=connection,
                    ))
                except product.ConflictingOperation as refused:
                    results.append(refused)

        threads = [threading.Thread(target=attempt, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        admitted = [r for r in results if isinstance(r, agent.AgentAdmission)]
        self.assertEqual(len(admitted), 1)
        self.assertEqual(len(results), 4)

    def test_an_ungranted_target_is_not_granted(self):
        with self.assertRaises(product.ActionNotGranted):
            self.submit("ch_south_1001")

    def test_an_invalid_intent_reference_is_refused(self):
        for intent in ("", " padded", "has space", "x" * 65, "*"):
            with self.subTest(intent=intent), \
                    self.assertRaises(product.RequestInvalid):
                self.submit(self.fresh(), intent)

    # -- visibility -----------------------------------------------------

    def test_an_agent_sees_only_operations_it_requested(self):
        client, csrf = self.api.signed_in(REQUESTER)
        response = client.post("/actions", headers={CSRF_HEADER: csrf}, json={
            "request_key": "rk-" + uuid.uuid4().hex[:16],
            "action_type": "refund_payment",
            "provider_account_id": "mock-payments-account",
            "payload": {"charge_id": "ch_north_1001", "amount_minor": 100},
        })
        self.assertIn(response.status_code, (200, 201), response.text)
        human_operation = uuid.UUID(response.json()["operation_id"])
        south = self.submit("ch_south_1002", who=SOUTH_AGENT).operation_id

        # Same tenant but a person's request, another tenant's agent, and
        # an id that does not exist: all look the same.
        for operation_id in (human_operation, south, uuid.uuid4()):
            with self.subTest(operation=operation_id):
                with self.assertRaises(lifecycle.UnknownOperation):
                    self.view(operation_id)
                with self.assertRaises(lifecycle.UnknownOperation):
                    agent.request_verification(
                        self.mcp, self.api.schema,
                        key=self.keys[NORTH_AGENT], operation_id=operation_id,
                    )
                with self.assertRaises(lifecycle.UnknownOperation):
                    agent.propose(
                        self.mcp, self.api.schema,
                        key=self.keys[NORTH_AGENT], operation_id=operation_id,
                        action="REVERIFY", rationale="look again",
                    )

        # A human investigator still sees the agent's work in the tenant.
        mine = self.submit(self.fresh(),
                           intent="visible-to-investigator").operation_id
        client, _ = self.api.signed_in(INVESTIGATOR)
        self.assertEqual(client.get(f"/actions/{mine}").status_code, 200)

    # -- scopes ---------------------------------------------------------

    def test_scopes_narrow_what_a_key_can_do(self):
        principal = self.service_principal(
            "demo-north-scoped-agent",
            grants=(("mock-payments", "mock-payments-account",
                     "refund_payment", "ch_north_1001", "v1"),),
        )
        submit_only = self.key_for(principal, scopes=("submit",)).key
        investigate_only = self.key_for(principal, scopes=("investigate",)).key

        admitted = self.submit("ch_north_1001", key=submit_only)
        # Reading needs no particular scope: both keys read their own.
        self.view(admitted.operation_id, key=submit_only)
        self.view(admitted.operation_id, key=investigate_only)

        with self.assertRaises(product.RoleRequired):
            agent.request_verification(
                self.mcp, self.api.schema, key=submit_only,
                operation_id=admitted.operation_id,
            )
        with self.assertRaises(product.RoleRequired):
            agent.propose(
                self.mcp, self.api.schema, key=submit_only,
                operation_id=admitted.operation_id, action="REVERIFY",
                rationale="x",
            )
        with self.assertRaises(product.RoleRequired):
            self.submit("ch_north_1001", key=investigate_only)

    def test_a_scope_without_the_role_is_still_refused(self):
        # The key's scope narrows roles; it never adds one.
        principal = self.service_principal(
            "demo-north-reader-agent", roles=("investigator",),
            grants=(("mock-payments", "mock-payments-account",
                     "refund_payment", "ch_north_1001", "v1"),),
        )
        key = self.key_for(principal, scopes=("investigate", "submit")).key
        with self.assertRaises(product.RoleRequired):
            self.submit("ch_north_1001", key=key)

    # -- credentials ----------------------------------------------------

    def test_bad_keys_are_unauthenticated_and_leave_no_rows(self):
        principal = self.service_principal("demo-north-temp-agent")
        expired = self.key_for(principal, lifetime=timedelta(seconds=1))
        revoked = self.key_for(principal)
        disabled_principal = self.service_principal("demo-north-off-agent")
        disabled = self.key_for(disabled_principal)
        with self.api.admin.transaction():
            revoke_agent_key(self.api.admin, self.api.schema, revoked.key_id)
            self.api.admin.execute(sql.SQL(
                "INSERT INTO {} (principal_id) VALUES (%s)"
            ).format(self.api.table("safeact_principal_disablements")),
                (disabled_principal,))
        time.sleep(1.2)

        sessions = self.count("safeact_sessions")
        calls = self.count("safeact_agent_call_sessions")
        cases = {
            "missing": None,
            "empty": "",
            "forged": "sak_" + secrets.token_hex(32),
            "uppercase": self.keys[NORTH_AGENT].upper(),
            "no prefix": self.keys[NORTH_AGENT][4:],
            "human session secret": secrets.token_hex(32),
            "expired": expired.key,
            "revoked": revoked.key,
            "disabled principal": disabled.key,
        }
        for label, key in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(product.Unauthenticated):
                    agent.operation(
                        self.mcp, self.api.schema, key=key,
                        operation_id=uuid.uuid4(),
                    )
                with self.assertRaises(product.Unauthenticated):
                    self.submit(POOL[-1], key=key)
        self.assertEqual(self.count("safeact_sessions"), sessions)
        self.assertEqual(self.count("safeact_agent_call_sessions"), calls)

    def test_a_human_session_secret_never_works_as_a_key_nor_a_key_as_a_session(self):
        client, _ = self.api.signed_in(REQUESTER)
        session = client.cookies.get(SESSION_COOKIE)
        with self.assertRaises(product.Unauthenticated):
            agent.operation(self.mcp, self.api.schema, key=session,
                            operation_id=uuid.uuid4())

        anonymous = self.api.client()
        anonymous.cookies.set(SESSION_COOKIE, self.keys[NORTH_AGENT])
        self.assertEqual(anonymous.get("/auth/me").status_code, 401)
        self.assertEqual(anonymous.get("/actions").status_code, 401)

    def test_reviewer_can_never_be_expressed_for_an_agent(self):
        principal = self.principal(NORTH_AGENT)
        with self.assertRaises(ValueError):
            self.key_for(principal, scopes=("reviewer",))

        # Straight into the table, as an administrator: the CHECK holds.
        for scopes in (["reviewer"], ["submit", "reviewer"], [], [None]):
            with self.subTest(scopes=scopes), \
                    self.assertRaises(psycopg.errors.CheckViolation):
                with self.api.admin.transaction():
                    self.api.admin.execute(sql.SQL(
                        "INSERT INTO {} (key_id, principal_id, key_sha256, "
                        "scopes, label, created_by, expires_at) VALUES "
                        "(gen_random_uuid(), %s, %s, %s, 'x', 'x', "
                        "now() + interval '1 day')"
                    ).format(self.api.table("safeact_agent_keys")),
                        (principal, secrets.token_bytes(32), scopes))

        # Never longer than 30 days.
        with self.assertRaises(ValueError):
            self.key_for(principal, lifetime=timedelta(days=31))
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.api.admin.transaction():
                self.api.admin.execute(sql.SQL(
                    "INSERT INTO {} (key_id, principal_id, key_sha256, "
                    "scopes, label, created_by, expires_at) VALUES "
                    "(gen_random_uuid(), %s, %s, '{{submit}}', 'x', 'x', "
                    "now() + interval '31 days')"
                ).format(self.api.table("safeact_agent_keys")),
                    (principal, secrets.token_bytes(32)))

        # Only a service principal holds a key; and still no reviewer grant.
        with self.assertRaises(psycopg.Error) as refused:
            self.key_for(self.principal(REQUESTER))
        self.assertEqual(refused.exception.sqlstate, lsql.IDENTITY_INVALID)
        with self.assertRaises(psycopg.Error) as refused:
            with self.api.admin.transaction():
                self.api.admin.execute(sql.SQL(
                    "INSERT INTO {} (principal_id, role) VALUES (%s, 'reviewer')"
                ).format(self.api.table("safeact_role_grants")), (principal,))
        self.assertEqual(refused.exception.sqlstate, lsql.IDENTITY_INVALID)

    def test_keys_and_revocations_are_append_only(self):
        principal = self.service_principal("demo-north-append-agent")
        issued = self.key_for(principal)
        for statement in (
            "UPDATE {} SET expires_at = now() + interval '20 days'",
            "DELETE FROM {}",
        ):
            with self.subTest(statement=statement), \
                    self.assertRaises(psycopg.Error):
                with self.api.admin.transaction():
                    self.api.admin.execute(sql.SQL(
                        statement + " WHERE key_id = %s"
                    ).format(self.api.table("safeact_agent_keys")),
                        (issued.key_id,))
        self.assertNotIn(issued.key, repr(issued))

    # -- proposals ------------------------------------------------------

    def test_agent_proposals_are_limited_and_only_a_human_decides(self):
        admitted = self.submit(self.fresh(), "escalate-me")
        self.escalate(admitted.operation_id)
        key = self.keys[NORTH_AGENT]

        with self.assertRaises(product.NotEligible):
            agent.propose(self.mcp, self.api.schema, key=key,
                          operation_id=admitted.operation_id,
                          action="ATTEST_APPLIED", rationale="it happened")
        proposal = agent.propose(
            self.mcp, self.api.schema, key=key,
            operation_id=admitted.operation_id, action="REVERIFY",
            rationale="look again after the provider recovers",
        )
        self.assertEqual(proposal["action"], "REVERIFY")
        self.assertEqual(proposal["proposed_by"], str(self.principal(NORTH_AGENT)))

        # The MCP role has no decision function at all.
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.mcp.execute(sql.SQL(
                "SELECT {}.safeact_api_decide(%s, %s, %s, 'approve', %s, %s, NULL)"
            ).format(sql.Identifier(self.api.schema)), (
                key, key, proposal["proposal_id"], proposal["binding_digest"],
                uuid.uuid4(),
            ))

        # A human reviewer (neither proposer nor requester) decides once.
        client, csrf = self.api.signed_in(REVIEWER)
        response = client.post(
            f"/proposals/{proposal['proposal_id']}/decision",
            headers={CSRF_HEADER: csrf}, json={
                "decision": "approve",
                "binding_digest": proposal["binding_digest"],
                "decision_key": str(uuid.uuid4()),
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_verification_requests_go_through_the_contract_bounds(self):
        admitted = self.submit(self.fresh(), "verify-me")
        # Not escalated yet: the reconciler already owns the next lookup.
        with self.assertRaises(product.NotEligible):
            agent.request_verification(
                self.mcp, self.api.schema, key=self.keys[NORTH_AGENT],
                operation_id=admitted.operation_id,
            )

    # -- privileges -----------------------------------------------------

    def call(self, connection, name):
        arguments = lsql.ALL_FUNCTIONS[name]
        count = len([a for a in arguments.split(",") if a.strip()])
        return sql.SQL("SELECT {}.{}({})").format(
            sql.Identifier(self.api.schema), sql.Identifier(name),
            sql.SQL(", ").join(sql.SQL("NULL") for _ in range(count)),
        )

    def assert_denied(self, connection, query):
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            connection.execute(query)

    def test_the_mcp_role_may_execute_only_the_mcp_functions(self):
        for name in lsql.ALL_FUNCTIONS:
            if name in lsql.MCP_FUNCTIONS:
                continue
            with self.subTest(function=name):
                self.assert_denied(self.mcp, self.call(self.mcp, name))
        for table in ("safeact_operations", "safeact_agent_keys",
                      "safeact_sessions", "safeact_proposals"):
            with self.subTest(table=table):
                self.assert_denied(self.mcp, sql.SQL(
                    "SELECT 1 FROM {}"
                ).format(self.api.table(table)))

    def test_the_api_role_cannot_execute_the_mcp_functions(self):
        with self.api.connect_api() as api_connection:
            for name in lsql.MCP_FUNCTIONS:
                with self.subTest(function=name):
                    self.assert_denied(
                        api_connection, self.call(api_connection, name)
                    )


if __name__ == "__main__":
    unittest.main()
