"""The three section 07 journeys over real HTTP (M3, D6; A8 API side).

Opt-in: SAFEACT_RUN_API_E2E=1 (with the local PostgreSQL and Redis).
One disposable environment with e2e-scaled contracts and the synthetic
demo accounts; the whole stack (uvicorn API, relay, Celery worker over
Redis database 14, reconciler, advancer) runs in-process against mock
providers with the harness-only fault script in deploy.demo.run_stack.
Every assertion is about backend state: the API's answers, the
database, and the harness ledger of applied effects.
"""

import json
import os
import socket
import threading
import time
import unittest
import uuid

from concurrent.futures import ThreadPoolExecutor

import httpx2
import redis

from api.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from deploy.demo.bootstrap import (
    DEMO_ACCOUNTS, Environment, rebuild, teardown_statements,
)
from deploy.demo.profiles import E2E_PROFILES
from deploy.demo.run_stack import Stack
from storage.database import local_admin_settings, local_redis_url
from storage.state_projection import projection_drift

import psycopg
from psycopg import sql


ENABLED = os.environ.get("SAFEACT_RUN_API_E2E") == "1"
REDIS_DB = 14


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Person:
    """One signed-in browser, speaking plain HTTP to the local stack."""

    def __init__(self, base_url, username, password):
        self.username = username
        self.http = httpx2.Client(base_url=base_url, timeout=10)
        response = self.http.post(
            "/auth/login", json={"username": username, "password": password}
        )
        response.raise_for_status()
        # A browser sends Secure cookies to localhost; httpx does not, so
        # carry them over explicitly (the API's flags are unchanged).
        for name in (SESSION_COOKIE, CSRF_COOKIE):
            self.http.cookies.set(name, response.cookies[name])
        self.csrf = response.json()["csrf_token"]
        self.principal = response.json()["principal"]

    def get(self, path, **kwargs):
        return self.http.get(path, **kwargs)

    def post(self, path, body=None, **kwargs):
        headers = {CSRF_HEADER: self.csrf, **kwargs.pop("headers", {})}
        return self.http.post(path, json=body, headers=headers, **kwargs)

    def close(self):
        self.http.close()


def frames(lines):
    """Parse SSE lines into (event, id, data) frames as they arrive."""

    event = event_id = data = None
    for line in lines:
        if line == "":
            if event is not None:
                yield event, event_id, json.loads(data or "{}")
            event = event_id = data = None
        elif line.startswith(":"):
            continue
        else:
            name, _, value = line.partition(": ")
            if name == "event":
                event = value
            elif name == "id":
                event_id = value
            elif name == "data":
                data = value


@unittest.skipUnless(ENABLED, "API end-to-end tests are opt-in")
class JourneyTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.redis = redis.Redis.from_url(local_redis_url(REDIS_DB))
        cls.redis.flushdb()
        cls.addClassCleanup(cls.redis.flushdb)

        cls.admin = psycopg.connect(**local_admin_settings(), autocommit=True)
        cls.env = Environment.disposable(uuid.uuid4().hex)
        cls.addClassCleanup(cls.teardown_env)
        role_passwords, cls.passwords = rebuild(
            cls.admin, cls.env, profiles=E2E_PROFILES, accounts=DEMO_ACCOUNTS
        )
        roles = {
            "api": cls.env.api, "worker": cls.env.worker,
            "recoverer": cls.env.recoverer, "advancer": cls.env.advancer,
        }
        cls.stack = Stack(
            schema=cls.env.schema, roles=roles, passwords=role_passwords,
            port=free_port(), redis_db=REDIS_DB, poll_seconds=0.2,
            sse={"sse_poll_seconds": 0.1, "sse_heartbeat_seconds": 1.0,
                 "sse_max_seconds": 4.0},
        ).start()
        cls.addClassCleanup(cls.stack.stop)
        cls.people = {}

    @classmethod
    def teardown_env(cls):
        # The journeys leave the projection equal to the log.
        drift = projection_drift(cls.admin, cls.env.schema)
        cls.admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE usename = ANY(%s)", (list(cls.env.roles),),
        )
        with cls.admin.transaction():
            for statement in teardown_statements(cls.admin, cls.env):
                cls.admin.execute(statement)
        cls.admin.close()
        if drift:
            raise AssertionError(f"Current-state projection drift: {drift[:3]}")

    # -- helpers -------------------------------------------------------

    def person(self, username):
        if username not in self.people:
            self.people[username] = Person(
                self.stack.base_url, username, self.passwords[username]
            )
            self.addClassCleanup(self.people[username].close)
        return self.people[username]

    def submit(self, who, action, account, payload, key=None):
        response = self.person(who).post("/actions", {
            "request_key": key or "rk-" + uuid.uuid4().hex[:16],
            "action_type": action,
            "provider_account_id": account,
            "payload": payload,
        })
        self.assertIn(response.status_code, (200, 201), response.text)
        return response.json()["operation_id"]

    def refund(self, charge, who="demo-north-requester", key=None):
        return self.submit(
            who, "refund_payment", "mock-payments-account",
            {"charge_id": charge, "amount_minor": 1500}, key,
        )

    def crm_order(self, case):
        return self.submit(
            "demo-north-requester", "create_replacement_order",
            "mock-crm-account", {"case_id": case, "sku": "sku-replacement"},
        )

    def detail(self, operation_id, who="demo-north-investigator"):
        response = self.person(who).get(f"/actions/{operation_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def wait_state(self, operation_id, states, timeout=40):
        states = {states} if isinstance(states, str) else set(states)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.detail(operation_id)
            if current["state"] in states:
                return current
            time.sleep(0.2)
        self.fail(f"{operation_id} never reached {states}: {current['state']}")

    def provider_key(self, operation_id):
        return self.admin.execute(sql.SQL(
            "SELECT provider_idempotency_key FROM {}.safeact_operations "
            "WHERE operation_id = %s"
        ).format(sql.Identifier(self.env.schema)), (operation_id,)).fetchone()[0]

    def effects(self, operation_id):
        return self.stack.effects(operation_id, self.provider_key(operation_id))

    def horizon(self, who):
        with self.person(who).http.stream("GET", "/events") as response:
            for event, event_id, _ in frames(response.iter_lines()):
                if event == "reset":
                    return event_id
        self.fail("no reset event")

    def follow(self, who, after, until, timeout=40):
        """Follow SSE from ``after`` until ``until(changed_ids)`` holds,
        reconnecting with Last-Event-ID whenever a stream ends."""

        seen, cursor = set(), after
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            headers = {"Last-Event-ID": cursor} if cursor else {}
            with self.person(who).http.stream(
                "GET", "/events", headers=headers
            ) as response:
                self.assertEqual(response.status_code, 200)
                for event, event_id, data in frames(response.iter_lines()):
                    if event_id:
                        cursor = event_id
                    if event == "changes":
                        seen |= {c["operation_id"] for c in data["changes"]}
                        if until(seen):
                            return seen, cursor
                    if event in ("reconnect", "session_ended"):
                        break
        self.fail(f"feed never satisfied the condition; saw {seen}")

    # -- journey 1: submit and follow ------------------------------------

    def test_journey_1_submit_follow_and_refresh(self):
        who = "demo-north-requester"
        start = self.horizon(who)
        operation_id = self.refund("ch_north_1001")

        def done(seen):
            return (operation_id in seen
                    and self.detail(operation_id, who)["state"] == "SUCCEEDED")

        self.follow(who, start, done)
        first = self.detail(operation_id, who)
        self.assertEqual(first["outcome_class"], "confirmed")
        self.assertEqual(first["success_provenance"], "provider_verified")

        # A refresh (a new browser session) shows the same durable state.
        again = Person(self.stack.base_url, who, self.passwords[who])
        try:
            refreshed = again.get(f"/actions/{operation_id}").json()
        finally:
            again.close()
        self.assertEqual(
            {k: refreshed[k] for k in ("state", "seq", "attempts", "transitions")},
            {k: first[k] for k in ("state", "seq", "attempts", "transitions")},
        )
        self.assertEqual(self.effects(operation_id), 1)

    # -- journey 2: investigate uncertainty ------------------------------

    def test_journey_2_timeout_after_success_is_verified_not_redone(self):
        operation_id = self.refund("ch_north_1002")
        final = self.wait_state(operation_id, "SUCCEEDED")
        reasons = [t["reason"] for t in final["transitions"]]
        self.assertIn("ambiguous_no_response", reasons)
        self.assertEqual(len(final["attempts"]), 1)
        kinds = {o["evidence_kind"] for o in final["attempts"][0]["outcomes"]}
        self.assertIn("applied", kinds)
        self.assertEqual(self.effects(operation_id), 1)

    def test_journey_2_late_commit_stays_unknown_and_is_listed(self):
        operation_id = self.crm_order("case_north_2002")
        final = self.wait_state(operation_id, "ESCALATED_UNRESOLVED")
        self.assertEqual(final["outcome_class"], "unknown")
        kinds = {
            o["evidence_kind"] for a in final["attempts"] for o in a["outcomes"]
        }
        self.assertNotIn("proven_not_applied", kinds)
        self.assertIn("not_found", kinds)
        # The effect exists; nothing was resent to "make sure".
        self.assertEqual(self.effects(operation_id), 1)
        unresolved = self.person("demo-north-investigator").get(
            "/actions", params={"unresolved": True}
        ).json()["items"]
        self.assertIn(operation_id, {i["operation_id"] for i in unresolved})

    # -- journey 3: review a proposal -------------------------------------

    def test_journey_3_review_with_stale_and_fresh_approval(self):
        investigator = self.person("demo-north-investigator")
        reviewer = self.person("demo-north-reviewer")
        second = self.person("demo-north-reviewer2")

        email = self.submit(
            "demo-north-requester", "send_refund_confirmation",
            "mock-email-account",
            {"customer_id": "cust_north_3002", "template": "refund_done"},
        )
        self.wait_state(email, "ESCALATED_UNRESOLVED")
        proposal = investigator.post(f"/actions/{email}/proposals", {
            "action": "ATTEST_APPLIED",
            "rationale": "Provider dashboard shows the message delivered.",
            "attested_reference": "msg_delivered_e2e_1",
        }).json()

        # Two reviewers decide at once: one decision, one refusal.
        results = {}

        def review(person):
            results[person.username] = person.post(
                f"/proposals/{proposal['proposal_id']}/decision", {
                    "decision": "approve",
                    "binding_digest": proposal["binding_digest"],
                    "decision_key": str(uuid.uuid4()),
                })

        threads = [threading.Thread(target=review, args=(p,))
                   for p in (reviewer, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(r.status_code for r in results.values()), [200, 409])

        final = self.wait_state(email, "SUCCEEDED")
        self.assertEqual(final["success_provenance"], "human_attested")
        self.assertEqual(self.effects(email), 1)
        events = {t["reason"] for t in final["transitions"]}
        self.assertIn("reopen_recorded_evidence", events)

        # Stale: the reconciler keeps looking at an escalated CRM order,
        # so a proposal shown before a new lookup is refused.
        crm = self.crm_order("case_north_2002")
        self.wait_state(crm, "ESCALATED_UNRESOLVED")
        stale = investigator.post(f"/actions/{crm}/proposals", {
            "action": "ATTEST_APPLIED",
            "rationale": "Record visible in the CRM console.",
            "attested_reference": "crm_rec_e2e_1",
        }).json()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if investigator.get(
                f"/proposals/{stale['proposal_id']}"
            ).json()["status"] == "stale":
                break
            time.sleep(0.3)
        refused = reviewer.post(f"/proposals/{stale['proposal_id']}/decision", {
            "decision": "approve", "binding_digest": stale["binding_digest"],
            "decision_key": str(uuid.uuid4()),
        })
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["error"]["code"], "stale_approval")
        self.assertNotEqual(self.detail(crm)["state"], "SUCCEEDED")

    def test_journey_3_protected_retry_after_review(self):
        investigator = self.person("demo-north-investigator")
        reviewer = self.person("demo-north-reviewer")
        operation_id = self.crm_order("case_north_2003")
        self.wait_state(operation_id, "VERIFYING", timeout=20)

        proposal = investigator.post(f"/actions/{operation_id}/proposals", {
            "action": "PROTECTED_RETRY",
            "rationale": "Rejected before execution; the hold is cleared.",
        })
        self.assertEqual(proposal.status_code, 201, proposal.text)
        approved = reviewer.post(
            f"/proposals/{proposal.json()['proposal_id']}/decision", {
                "decision": "approve",
                "binding_digest": proposal.json()["binding_digest"],
                "decision_key": str(uuid.uuid4()),
            })
        self.assertEqual(approved.json()["outcome"], "approved", approved.text)

        final = self.wait_state(operation_id, "SUCCEEDED")
        self.assertEqual(len(final["attempts"]), 2)
        self.assertIn(
            "retry_approved_by_reviewer",
            {t["reason"] for t in final["transitions"]},
        )
        self.assertEqual(self.effects(operation_id), 1)

    # -- negative journeys ------------------------------------------------

    def test_replacement_example_tracks_separate_charge_order_and_inverse_effects(self):
        from examples.replacement import follow, sign_in

        requester = self.person("demo-north-requester")
        example_http = httpx2.Client(base_url=self.stack.base_url, timeout=10)
        self.addCleanup(example_http.close)
        sign_in(example_http, requester.username, self.passwords[requester.username])
        for case, expected in (("case_north_2001", "COMPLETED"),
                               ("case_north_2004", "COMPENSATED_VERIFIED"),
                               ("case_north_2002", "UNRESOLVED")):
            with self.subTest(case=case):
                request = {
                    "request_key": "example-e2e-" + uuid.uuid4().hex,
                    "customer_id": "cust_north_3001", "case_id": case,
                    "sku": "sku-example", "fee_minor": 500,
                    "payments_account_id": "mock-payments-account",
                    "crm_account_id": "mock-crm-account",
                }
                result = follow(example_http, request, wait_seconds=30)
                self.assertEqual(result["status"], expected)
                steps = {step["step"]: step for step in result["steps"]}
                self.assertEqual(self.effects(steps["charge"]["operation_id"]), 1)
                self.assertEqual(self.effects(steps["order"]["operation_id"]),
                                 0 if expected == "COMPENSATED_VERIFIED" else 1)
                if expected == "COMPENSATED_VERIFIED":
                    charge = steps["charge"]["operation_id"]
                    refund = steps["compensation"]["operation_id"]
                    self.assertNotEqual(charge, refund)
                    self.assertEqual(steps["compensation"]["compensates_operation_id"], charge)
                    self.assertEqual(self.effects(refund), 1)
                    refund_calls = self.stack.ledger.calls_for(self.provider_key(refund))
                    original_effect = self.stack.ledger.effects_for(self.provider_key(charge))[0]
                    self.assertEqual(refund_calls[0], {"charge_id": original_effect, "amount_minor": 500})
                else:
                    self.assertNotIn("compensation", steps)
                replay = requester.post("/workflows", request)
                self.assertEqual(replay.status_code, 200)
                self.assertEqual(replay.json()["workflow_id"], result["workflow_id"])
                conflict = requester.post("/workflows", {**request, "fee_minor": 501})
                self.assertEqual(conflict.status_code, 409)

    def test_denied_tenant_access(self):
        north = self.refund("ch_north_1003")
        south = self.person("demo-south-requester")
        start = self.horizon("demo-south-requester")
        self.assertEqual(south.get(f"/actions/{north}").status_code, 404)
        self.assertEqual(
            south.post(f"/actions/{north}/cancel").status_code, 404
        )
        mine = self.refund("ch_south_1001", who="demo-south-requester")
        seen, _ = self.follow(
            "demo-south-requester", start, lambda ids: mine in ids
        )
        self.assertNotIn(north, seen)

    def test_duplicate_submission_is_one_operation_and_one_effect(self):
        key = "rk-" + uuid.uuid4().hex[:16]
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda _: self.refund("ch_north_1004", key=key), range(4)))
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(set(ids)), 1)
        self.wait_state(ids[0], "SUCCEEDED")
        self.assertEqual(self.effects(ids[0]), 1)

    def test_connection_interruption_catches_up_from_last_event_id(self):
        who = "demo-north-investigator"
        start = self.horizon(who)
        before = self.refund("ch_north_1001")

        # Drop the connection after the submitted operation is observed.
        # An unrelated open transaction can hold the database-wide feed
        # horizon beyond one stream's lifetime. Honor reconnect events just
        # as the client does, retaining the cursor until changes are visible.
        seen, cursor = self.follow(who, start, lambda ids: before in ids)
        self.assertIn(before, seen)
        self.assertIsNotNone(cursor)

        # Work happens while no stream is open.
        missed = self.refund("ch_north_1004", key="rk-" + uuid.uuid4().hex[:16])
        seen, _ = self.follow(who, cursor, lambda ids: missed in ids)
        self.assertIn(missed, seen)
        # The stream is a hint; REST shows the same durable state.
        self.wait_state(missed, "SUCCEEDED")
        self.assertIn(before, {
            i["operation_id"] for i in self.person(who).get("/actions").json()["items"]
        })


if __name__ == "__main__":
    unittest.main()
