"""Authorized submission, reads, workflows and cancellation (M3, D3).

Through the real FastAPI app as the restricted API role, on a fresh
disposable environment per test class. Requires
SAFEACT_RUN_DB_INTEGRATION=1 and SAFEACT_RUN_DISPOSABLE_DB_TESTS=1.
"""

import json
import os
import threading
import unittest
import uuid

import psycopg
from psycopg import sql

from api.cookies import CSRF_HEADER
from core.lifecycle import LifecycleState as S
from storage import durable_lifecycle as lifecycle
from storage import durable_lifecycle_sql as lsql
from storage import product_api as product
from storage.experimental_durable_execution import (
    ExecutionStatus, ExecutionTask, ExecutionWorker, relay_once,
)
from storage.experimental_recovery_worker import Reconciler, account_operations
from storage.identity_admin import create_principal
from tests.integration.api_fixture import ApiEnvironment


ENABLED = (
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1"
)

REQUESTER = "demo-north-requester"
INVESTIGATOR = "demo-north-investigator"
REVIEWER = "demo-north-reviewer"
SOUTH = "demo-south-requester"


def key():
    return "rk-" + uuid.uuid4().hex[:16]


def refund(charge="ch_north_1001", amount=1200, **changes):
    body = {
        "request_key": key(),
        "action_type": "refund_payment",
        "provider_account_id": "mock-payments-account",
        "payload": {"charge_id": charge, "amount_minor": amount},
    }
    body.update(changes)
    return body


def workflow_body(**changes):
    body = {
        "request_key": "wf-" + uuid.uuid4().hex[:16],
        "customer_id": "cust_north_3001",
        "case_id": "case_north_2001",
        "sku": "sku-replacement-1",
        "fee_minor": 500,
        "payments_account_id": "mock-payments-account",
        "crm_account_id": "mock-crm-account",
    }
    body.update(changes)
    return body


class Collector:
    def __init__(self):
        self.tasks = []

    def publish(self, task):
        self.tasks.append(task)


@unittest.skipUnless(ENABLED, "PostgreSQL integration tests are opt-in")
class ApiActionCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.api = ApiEnvironment().start()
        cls.addClassCleanup(cls.api.cleanup)
        cls.north, cls.north_csrf = cls.api.signed_in(REQUESTER)
        cls.south, cls.south_csrf = cls.api.signed_in(SOUTH)
        cls.investigator, cls.investigator_csrf = cls.api.signed_in(INVESTIGATOR)
        cls.reviewer, cls.reviewer_csrf = cls.api.signed_in(REVIEWER)

    def submit(self, body, client=None, csrf=None):
        client = client or self.north
        csrf = csrf or (self.north_csrf if client is self.north else None)
        return client.post("/actions", json=body, headers={CSRF_HEADER: csrf})

    def admitted(self, body=None):
        response = self.submit(body or refund())
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["operation_id"]

    def cancel(self, operation_id, client=None, csrf=None):
        client = client or self.north
        csrf = csrf or self.north_csrf
        return client.post(
            f"/actions/{operation_id}/cancel", headers={CSRF_HEADER: csrf}
        )

    def operation_row(self, operation_id):
        return self.api.admin.execute(sql.SQL(
            "SELECT tenant_id, connector_id, provider_idempotency_key "
            "FROM {} WHERE operation_id = %s"
        ).format(self.api.table("safeact_operations")),
            (operation_id,)).fetchone()

    def state(self, operation_id):
        return self.api.admin.execute(sql.SQL(
            "SELECT state, seq FROM {} WHERE operation_id = %s"
        ).format(self.api.table(lsql.CURRENT_STATE_VIEW)),
            (operation_id,)).fetchone()


class SubmissionTests(ApiActionCase):

    def test_submission_records_one_bundle_with_server_identity(self):
        operation_id = self.admitted()
        tenant, connector, provider_key = self.operation_row(operation_id)
        self.assertEqual(tenant, "tenant-north")
        self.assertEqual(connector, "mock-payments")
        self.assertRegex(provider_key, r"^sa1_[0-9a-f]{64}$")
        self.assertEqual(self.api.count(
            "safeact_outbox_events", sql.SQL("operation_id = %s"),
            (operation_id,),
        ), 1)
        actor = self.api.admin.execute(sql.SQL(
            "SELECT actor_id FROM {} WHERE operation_id = %s "
            "AND event_type = 'INTENT_RECORDED'"
        ).format(self.api.table("safeact_audit_events")),
            (operation_id,)).fetchone()[0]
        self.assertEqual(actor, str(self.api.principal_id(REQUESTER)))

    def test_crm_operations_get_no_provider_key(self):
        operation_id = self.admitted({
            "request_key": key(),
            "action_type": "create_replacement_order",
            "provider_account_id": "mock-crm-account",
            "payload": {"case_id": "case_north_2002", "sku": "sku-9"},
        })
        self.assertEqual(self.operation_row(operation_id)[1:], ("mock-crm", None))

    def test_resubmission_is_one_operation_and_a_change_conflicts(self):
        body = refund()
        first = self.submit(body)
        again = self.submit(body)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(first.json()["operation_id"], again.json()["operation_id"])
        self.assertFalse(again.json()["created"])

        for change in (
            {"payload": {"charge_id": "ch_north_1001", "amount_minor": 999}},
            {"payload": {"charge_id": "ch_north_1002", "amount_minor": 1200}},
            {"contract_version": "v1-eventual"},
        ):
            with self.subTest(change=change):
                response = self.submit(dict(body, **change))
                self.assertIn(response.status_code, (403, 409))
                if response.status_code == 409:
                    self.assertEqual(
                        response.json()["error"]["code"], "request_conflict"
                    )

    def test_concurrent_identical_submissions_make_one_operation(self):
        body = refund(charge="ch_north_1003")
        results = []

        def go():
            client, csrf = self.api.signed_in(REQUESTER)
            results.append(client.post(
                "/actions", json=body, headers={CSRF_HEADER: csrf}
            ))

        threads = [threading.Thread(target=go) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(r.status_code for r in results), [200] * 5 + [201])
        self.assertEqual(len({r.json()["operation_id"] for r in results}), 1)
        self.assertEqual(self.api.count(
            "safeact_operations", sql.SQL("request_key = %s"),
            (body["request_key"],),
        ), 1)

    def test_body_cannot_choose_tenant_actor_or_connector(self):
        for extra in (
            {"tenant_id": "tenant-south"},
            {"actor_id": "someone"},
            {"connector_id": "mock-email"},
            {"resource_id": "ch_south_1001"},
            {"provider_idempotency_key": "sa1_x"},
        ):
            with self.subTest(extra=extra):
                response = self.submit(dict(refund(), **extra))
                self.assertEqual(response.status_code, 422)

    def test_duplicate_json_keys_are_refused(self):
        raw = (
            '{"request_key": "%s", "action_type": "refund_payment", '
            '"provider_account_id": "mock-payments-account", '
            '"payload": {"charge_id": "ch_north_1001", "amount_minor": 1, '
            '"amount_minor": 99999}}' % key()
        )
        response = self.north.post(
            "/actions", content=raw,
            headers={CSRF_HEADER: self.north_csrf,
                     "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 422)

    def test_payloads_are_validated_per_action(self):
        for payload in (
            {"charge_id": "ch_north_1001"},
            {"charge_id": "ch_north_1001", "amount_minor": 12.5},
            {"charge_id": "ch_north_1001", "amount_minor": "12"},
            {"charge_id": "ch_north_1001", "amount_minor": -1},
            {"charge_id": "ch north", "amount_minor": 1},
            {"charge_id": "ch_north_1001", "amount_minor": 1, "note": "x"},
        ):
            with self.subTest(payload=payload):
                response = self.submit(refund() | {"payload": payload})
                self.assertEqual(response.status_code, 422)
        self.assertEqual(
            self.submit(refund(action_type="charge_shipping_fee")).status_code,
            422,
        )

    def test_grants_are_exact(self):
        cases = {
            "other tenant's resource": refund(charge="ch_south_1001"),
            "ungranted resource": refund(charge="ch_north_9999"),
            "other account": refund(provider_account_id="other-account"),
            "other contract": refund(contract_version="v1-eventual"),
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                response = self.submit(body)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error"]["code"], "not_granted")

    def test_only_requesters_submit_and_only_with_csrf(self):
        for client, csrf in (
            (self.investigator, self.investigator_csrf),
            (self.reviewer, self.reviewer_csrf),
        ):
            response = client.post(
                "/actions", json=refund(), headers={CSRF_HEADER: csrf}
            )
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["error"]["code"], "forbidden")

        response = self.north.post("/actions", json=refund())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "csrf_refused")
        anonymous = self.api.client().post("/actions", json=refund())
        self.assertEqual(anonymous.status_code, 401)

    def test_reserved_workflow_keys_are_refused(self):
        response = self.submit(refund(request_key="wf:" + uuid.uuid4().hex))
        self.assertIn(response.status_code, (422,))

    def test_api_role_cannot_bypass_the_functions(self):
        with self.api.connect_api() as api:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                api.execute(sql.SQL(
                    "SELECT * FROM {}.safeact_api_admit(gen_random_uuid(), "
                    "'tenant-south', 'k', 'a', 'r', 'p', 'c', 'v', '{{}}')"
                ).format(sql.Identifier(self.api.schema)))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                api.execute(sql.SQL(
                    "INSERT INTO {} (operation_id) VALUES (gen_random_uuid())"
                ).format(self.api.table("safeact_operations")))


class ReadTests(ApiActionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ids = []
        for charge in ("ch_north_1001", "ch_north_1002", "ch_north_1003"):
            response = cls.north.post(
                "/actions", json=refund(charge=charge),
                headers={CSRF_HEADER: cls.north_csrf},
            )
            cls.ids.append(response.json()["operation_id"])
        cls.south_id = cls.south.post(
            "/actions", json=refund(charge="ch_south_1001"),
            headers={CSRF_HEADER: cls.south_csrf},
        ).json()["operation_id"]

    def test_other_tenant_is_not_found_everywhere(self):
        for path in (
            f"/actions/{self.south_id}",
            f"/actions/{uuid.uuid4()}",
        ):
            with self.subTest(path=path):
                response = self.north.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()["error"]["code"], "not_found")
        self.assertEqual(self.cancel(self.south_id).status_code, 404)
        listed = {i["operation_id"] for i in self.north.get("/actions").json()["items"]}
        self.assertNotIn(self.south_id, listed)
        self.assertTrue(set(self.ids) <= listed)
        south = {i["operation_id"] for i in self.south.get("/actions").json()["items"]}
        self.assertEqual(south, {self.south_id})

    def test_requesters_see_only_their_own_but_investigators_see_the_tenant(self):
        password = "second-requester-pw-1"
        with self.api.admin.transaction():
            create_principal(
                self.api.admin, self.api.schema, tenant_id="tenant-north",
                username="demo-north-requester-two", display_name="Two",
                roles=("requester",), password=password,
            )
        other = self.api.client()
        other.post("/auth/login", json={
            "username": "demo-north-requester-two", "password": password,
        })
        self.assertEqual(other.get(f"/actions/{self.ids[0]}").status_code, 404)
        self.assertEqual(other.get("/actions").json()["items"], [])

        for client in (self.investigator, self.reviewer):
            listed = {i["operation_id"] for i in client.get("/actions").json()["items"]}
            self.assertTrue(set(self.ids) <= listed)
            self.assertNotIn(self.south_id, listed)
            self.assertEqual(
                client.get(f"/actions/{self.ids[0]}").status_code, 200
            )

    def test_detail_separates_confirmed_from_unknown(self):
        detail = self.north.get(f"/actions/{self.ids[0]}").json()
        self.assertEqual(detail["state"], "RECORDED")
        self.assertEqual(detail["outcome_class"], "in_progress")
        self.assertEqual(detail["payload"], {
            "charge_id": "ch_north_1001", "amount_minor": 1200,
        })
        self.assertEqual(detail["contract"]["side_effect_class"], "reversible_payment")
        self.assertTrue(detail["contract"]["native_idempotency"])
        self.assertTrue(detail["provider_key_protected"])
        self.assertEqual(detail["attempts"], [])
        self.assertEqual(detail["transitions"], [])
        self.assertEqual(detail["requested_by"], str(self.api.principal_id(REQUESTER)))
        # 0019: when the next check happens, read from the same rules.
        self.assertEqual(detail["next_check"]["kind"], "awaiting_dispatch")
        self.assertEqual(set(detail["next_check"]),
                         {"kind", "not_before", "deadline", "basis"})

    def test_pagination_and_filters(self):
        page = self.north.get("/actions", params={"limit": 2}).json()
        self.assertEqual(len(page["items"]), 2)
        self.assertIsNotNone(page["next_before"])
        rest = self.north.get("/actions", params={
            "limit": 2, "before": page["next_before"],
            "before_id": page["next_before_id"],
        }).json()
        seen = [i["operation_id"] for i in page["items"] + rest["items"]]
        self.assertEqual(len(seen), len(set(seen)))

        none = self.north.get("/actions", params={"state": "SUCCEEDED"}).json()
        self.assertEqual(none["items"], [])
        unresolved = self.north.get("/actions", params={"unresolved": True}).json()
        self.assertEqual(unresolved["items"], [])
        crm = self.north.get("/actions", params={"connector": "mock-crm"}).json()
        self.assertTrue(all(i["connector_id"] == "mock-crm" for i in crm["items"]))

        self.assertEqual(
            self.north.get("/actions", params={"before": page["next_before"]}).status_code,
            422,
        )

    def test_grants_lists_only_the_principals_own(self):
        north = self.north.get("/grants").json()
        self.assertTrue(north)
        self.assertTrue(all("north" in g["resource_id"] for g in north))
        self.assertEqual(self.investigator.get("/grants").json(), [])


class CancellationTests(ApiActionCase):

    def test_requester_cancels_before_claim(self):
        operation_id = self.admitted()
        response = self.cancel(operation_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "outcome": "cancelled", "state": "CANCELLED_BEFORE_CLAIM",
            "seq": 1, "undone": False,
        })
        detail = self.north.get(f"/actions/{operation_id}").json()
        self.assertEqual(detail["outcome_class"], "cancelled")
        self.assertEqual(detail["transitions"][0]["reason"], "cancelled_before_claim")
        self.assertEqual(len(detail["cancellation_requests"]), 1)

        again = self.cancel(operation_id).json()
        self.assertEqual(again["outcome"], "refused_already_final")

    def test_investigator_may_cancel_reviewer_may_not(self):
        operation_id = self.admitted()
        refused = self.cancel(operation_id, self.reviewer, self.reviewer_csrf)
        self.assertEqual(refused.status_code, 403)
        accepted = self.cancel(
            operation_id, self.investigator, self.investigator_csrf
        )
        self.assertEqual(accepted.json()["outcome"], "cancelled")

    def test_after_a_claim_cancel_is_recorded_and_refused(self):
        operation_id = self.admitted()
        with self.api.connect_role(self.api.env.worker) as worker:
            lifecycle.claim(worker, self.api.schema, operation_id, lease_seconds=30)
            worker.commit()

        response = self.cancel(operation_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["outcome"], "refused_may_have_dispatched")
        self.assertFalse(response.json()["undone"])
        self.assertEqual(self.state(operation_id), ("CLAIMED", 1))
        refusals = self.api.rows(sql.SQL(
            "SELECT details FROM {} WHERE operation_id = %s "
            "AND event_type = 'CANCELLATION_REQUEST_REFUSED'"
        ).format(self.api.table("safeact_audit_events")), (operation_id,))
        self.assertEqual(refusals[0][0]["reason"], "may_have_dispatched")

    def test_workflow_steps_are_never_cancelled_here(self):
        created = self.north.post(
            "/workflows", json=workflow_body(),
            headers={CSRF_HEADER: self.north_csrf},
        ).json()
        response = self.cancel(created["charge_operation_id"])
        self.assertEqual(response.json()["outcome"], "refused_workflow_step")
        self.assertEqual(self.state(created["charge_operation_id"]), ("RECORDED", 0))

    def test_cancel_and_claim_race_has_exactly_one_winner(self):
        for _ in range(12):
            operation_id = self.admitted()
            barrier = threading.Barrier(2)
            outcome = {}

            def do_cancel():
                with self.api.connect_api() as api:
                    api.autocommit = False
                    barrier.wait()
                    secret = self.north.cookies.get("__Host-safeact_session")
                    result = product.cancel(
                        api, self.api.schema, secret=secret,
                        csrf=self.north_csrf, operation_id=operation_id,
                    )
                    api.commit()
                    outcome["cancel"] = result.outcome

            def do_claim():
                with self.api.connect_role(self.api.env.worker) as worker:
                    barrier.wait()
                    try:
                        lifecycle.claim(
                            worker, self.api.schema, operation_id,
                            lease_seconds=30,
                        )
                        worker.commit()
                        outcome["claim"] = "claimed"
                    except lifecycle.LifecycleRejected:
                        worker.rollback()
                        outcome["claim"] = "refused"

            threads = [threading.Thread(target=do_cancel),
                       threading.Thread(target=do_claim)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            state, seq = self.state(operation_id)
            if outcome["cancel"] == "cancelled":
                self.assertEqual(outcome["claim"], "refused")
                self.assertEqual((state, seq), ("CANCELLED_BEFORE_CLAIM", 1))
                self.assertEqual(self.api.count(
                    "safeact_attempts", sql.SQL("operation_id = %s"),
                    (operation_id,),
                ), 0)
            else:
                self.assertEqual(outcome["cancel"], "refused_may_have_dispatched")
                self.assertEqual(outcome["claim"], "claimed")
                self.assertEqual((state, seq), ("CLAIMED", 1))

    def test_cancelled_work_is_never_relayed_claimed_or_reconciled(self):
        operation_id = self.admitted()
        self.cancel(operation_id)
        outbox_id = self.api.admin.execute(sql.SQL(
            "SELECT outbox_id FROM {} WHERE operation_id = %s"
        ).format(self.api.table("safeact_outbox_events")),
            (operation_id,)).fetchone()[0]

        with self.api.connect_role(self.api.env.worker) as worker:
            collector = Collector()
            relay_once(worker, self.api.schema, collector)
            self.assertNotIn(operation_id, {str(t.operation_id) for t in collector.tasks})
            claimable = lifecycle.claimable_operations(
                worker, self.api.schema, limit=100
            )
            worker.commit()
            self.assertNotIn(operation_id, {str(o) for o in claimable})

        execution = ExecutionWorker(
            connect=lambda: self.api.connect_role(self.api.env.worker),
            schema=self.api.schema, connectors=(),
        ).execute(ExecutionTask(uuid.UUID(operation_id), outbox_id, uuid.uuid4()))
        self.assertEqual(execution.status, ExecutionStatus.NOT_CLAIMABLE)

        Reconciler(
            connect=lambda: self.api.connect_role(self.api.env.recoverer),
            schema=self.api.schema, connectors=(),
        ).reconcile_once()
        self.assertEqual(self.state(operation_id), ("CANCELLED_BEFORE_CLAIM", 1))

    def test_append_refuses_a_cancellation_after_any_history(self):
        operation_id = self.admitted()
        with self.api.connect_role(self.api.env.worker) as worker:
            lifecycle.claim(worker, self.api.schema, operation_id, lease_seconds=30)
            worker.commit()
        with self.assertRaises(psycopg.Error) as caught:
            self.api.admin.execute(sql.SQL(
                "SELECT {}.safeact_lifecycle_append(%s, 1, 'RECORDED', "
                "'CANCELLED_BEFORE_CLAIM', gen_random_uuid(), NULL, NULL, "
                "'cancelled_before_claim', '{{}}'::jsonb, 'X')"
            ).format(sql.Identifier(self.api.schema)), (operation_id,))
        self.assertEqual(caught.exception.sqlstate, lsql.ILLEGAL_TRANSITION)

    def test_accounting_keeps_cancelled_work_in_its_own_bucket(self):
        env = ApiEnvironment().start()
        try:
            client, csrf = env.signed_in(REQUESTER)
            ids = [
                client.post("/actions", json=refund(charge=c),
                            headers={CSRF_HEADER: csrf}).json()["operation_id"]
                for c in ("ch_north_1001", "ch_north_1002", "ch_north_1003")
            ]
            client.post(f"/actions/{ids[0]}/cancel", headers={CSRF_HEADER: csrf})
            with env.connect_role(env.env.recoverer) as recoverer:
                accounting = account_operations(recoverer, env.schema)
            self.assertEqual(accounting.total, 3)
            self.assertEqual(accounting.cancelled_before_claim, 1)
            self.assertEqual(accounting.in_flight, 2)
            self.assertEqual(accounting.completed, 0)
            self.assertEqual(accounting.unresolved, 0)
            self.assertEqual(accounting.by_state[S.CANCELLED_BEFORE_CLAIM], 1)
        finally:
            env.cleanup()


class WorkflowTests(ApiActionCase):

    def create(self, body, client=None, csrf=None):
        client = client or self.north
        return client.post(
            "/workflows", json=body,
            headers={CSRF_HEADER: csrf or self.north_csrf},
        )

    def test_creation_is_bound_to_the_requester_and_idempotent(self):
        body = workflow_body()
        first = self.create(body)
        self.assertEqual(first.status_code, 201, first.text)
        again = self.create(body)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(first.json()["workflow_id"], again.json()["workflow_id"])

        created_by = self.api.admin.execute(sql.SQL(
            "SELECT created_by, plan FROM {} WHERE workflow_id = %s"
        ).format(self.api.table("safeact_workflows")),
            (first.json()["workflow_id"],)).fetchone()
        self.assertEqual(created_by[0], str(self.api.principal_id(REQUESTER)))
        self.assertEqual(json.loads(created_by[1]["order"]["payload_canonical"]), {
            "case_id": "case_north_2001", "sku": "sku-replacement-1",
        })

        changed = self.create(dict(body, sku="sku-other"))
        self.assertEqual(changed.status_code, 409)

        read = self.north.get(f"/workflows/{first.json()['workflow_id']}").json()
        self.assertEqual(read["status"], "IN_PROGRESS")
        self.assertEqual([s["step"] for s in read["steps"]], ["charge"])

    def test_both_steps_must_be_granted(self):
        for change in (
            {"customer_id": "cust_north_9999"},
            {"case_id": "case_north_9999"},
            {"customer_id": "cust_south_3001"},
        ):
            with self.subTest(change=change):
                response = self.create(workflow_body(**change))
                self.assertEqual(response.status_code, 403)

    def test_replay_binds_every_charge_input_even_when_alternatives_are_granted(self):
        principal = self.api.principal_id(REQUESTER)
        for account, version in (("mock-payments-account", "v1-eventual"),
                                 ("other-payments-account", "v1")):
            self.api.admin.execute(sql.SQL(
                "INSERT INTO {} (principal_id, connector_id, provider_account_id, "
                "action_type, resource_id, contract_version) VALUES (%s, 'mock-payments', "
                "%s, 'charge_shipping_fee', 'cust_north_3001', %s) ON CONFLICT DO NOTHING"
            ).format(self.api.table("safeact_action_grants")), (principal, account, version))
        body = workflow_body()
        first = self.create(body)
        self.assertEqual(first.status_code, 201, first.text)
        for change in ({"fee_minor": 501}, {"customer_id": "cust_north_3002"},
                       {"charge_contract": "v1-eventual"},
                       {"payments_account_id": "other-payments-account"},
                       {"case_id": "case_north_2002"}, {"sku": "another-sku"},
                       {"compensation_contract": "v1-eventual"}):
            with self.subTest(change=change):
                response = self.create({**body, **change})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["error"]["code"], "request_conflict")
        replay = self.create(body)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["charge_operation_id"], first.json()["charge_operation_id"])
        detail = self.north.get(f"/actions/{first.json()['charge_operation_id']}").json()
        self.assertEqual(detail["payload"], {"customer_id": body["customer_id"], "amount_minor": 500})

    def test_concurrent_changed_fees_admit_one_workflow_and_one_charge(self):
        body = workflow_body()
        barrier = threading.Barrier(2)
        results = []

        def create(fee):
            with self.api.client() as client:
                client.cookies.update(self.north.cookies)
                barrier.wait(timeout=10)
                response = self.create({**body, "fee_minor": fee}, client=client)
                results.append((fee, response))

        threads = [threading.Thread(target=create, args=(fee,)) for fee in (500, 501)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(response.status_code for _, response in results), [201, 409])
        fee, winner = next((fee, response) for fee, response in results if response.status_code == 201)
        workflow_id = winner.json()["workflow_id"]
        self.assertEqual(self.api.count("safeact_workflows", sql.SQL("request_key = %s"), (body["request_key"],)), 1)
        self.assertEqual(self.api.count("safeact_workflow_steps", sql.SQL("workflow_id = %s"), (workflow_id,)), 1)
        detail = self.north.get(f"/actions/{winner.json()['charge_operation_id']}").json()
        self.assertEqual(detail["payload"]["amount_minor"], fee)

    def test_upgrade_existing_workflow_preserves_exact_replay_and_function_acl(self):
        from storage.lifecycle_sql import v0021

        # Reproduce a pre-fix bundle in this disposable schema, then upgrade.
        for statement in v0021.downgrade_statements(self.api.schema):
            self.api.admin.execute(statement)
        try:
            body = workflow_body()
            first = self.create(body)
            self.assertEqual(first.status_code, 201)
            self.assertEqual(self.create({**body, "fee_minor": 501}).status_code, 200)
            ownership = self.api.admin.execute(
                "SELECT p.proowner, p.proacl FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = %s AND p.proname = 'safeact_api_create_workflow'",
                (self.api.schema,),
            ).fetchone()
        finally:
            for statement in v0021.upgrade_statements(self.api.schema):
                self.api.admin.execute(statement)
        self.assertEqual(self.create(body).json()["workflow_id"], first.json()["workflow_id"])
        self.assertEqual(self.create({**body, "fee_minor": 501}).status_code, 409)
        self.assertEqual(self.api.admin.execute(
            "SELECT p.proowner, p.proacl FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = %s AND p.proname = 'safeact_api_create_workflow'",
            (self.api.schema,),
        ).fetchone(), ownership)

    def test_other_tenant_and_other_roles(self):
        created = self.create(workflow_body()).json()
        self.assertEqual(
            self.south.get(f"/workflows/{created['workflow_id']}").status_code, 404
        )
        self.assertEqual(
            self.investigator.get(f"/workflows/{created['workflow_id']}").status_code,
            200,
        )
        refused = self.investigator.post(
            "/workflows", json=workflow_body(),
            headers={CSRF_HEADER: self.investigator_csrf},
        )
        self.assertEqual(refused.status_code, 403)


if __name__ == "__main__":
    unittest.main()
