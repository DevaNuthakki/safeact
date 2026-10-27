"""Every concurrent caller must receive a replay or a declared conflict.

Barrier-synchronized requests exercise both tenant/request and native-provider
key indexes. Futures propagate all worker errors to the owning test.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import sqlalchemy as sa
from psycopg import sql

from api.cookies import CSRF_HEADER
from storage.database import local_database_url
from storage.submission import IntentSubmission, SubmissionConflict, record_intent
from tests.integration.test_api_actions import ApiActionCase, refund


class AdmissionReplayRaceTests(ApiActionCase):
    def test_all_simultaneous_http_callers_get_one_identity_and_complete_bundle(self):
        for batch in range(4):
            with self.subTest(batch=batch):
                body = refund(charge="ch_north_1004")
                barrier = Barrier(6)

                def go(_):
                    with self.api.client() as client:
                        client.cookies.update(self.north.cookies)
                        barrier.wait(timeout=10)
                        return client.post("/actions", json=body, headers={CSRF_HEADER: self.north_csrf})

                with ThreadPoolExecutor(max_workers=6) as pool:
                    results = list(pool.map(go, range(6)))
                self.assertEqual(sorted(r.status_code for r in results), [200] * 5 + [201])
                self.assertEqual(len({r.json()["operation_id"] for r in results}), 1)
                operation_id = results[0].json()["operation_id"]
                for table in ("safeact_operations", "safeact_audit_events", "safeact_outbox_events"):
                    self.assertEqual(self.api.count(table, sql.SQL("operation_id = %s"), (operation_id,)), 1)
                conflict = self.north.post("/actions", json={**body,
                    "payload": {**body["payload"], "amount_minor": 1501}}, headers={CSRF_HEADER: self.north_csrf})
                self.assertEqual(conflict.status_code, 409)

    def engine(self):
        engine = sa.create_engine(local_database_url(), connect_args={
            "options": f"-c search_path={self.api.schema},public -c statement_timeout=10000",
        })
        self.addCleanup(engine.dispose)
        return engine

    def request(self):
        return IntentSubmission(
            tenant_id="synthetic-tenant", request_key="race-" + uuid4().hex,
            actor_id="test-actor", action_type="refund_payment", resource_id="synthetic-charge",
            provider_account_id="mock-payments-account", connector_id="mock-payments", contract_version="v1",
            payload={"charge_id": "synthetic-charge", "amount_minor": 500},
        )

    def test_all_internal_callers_observe_one_complete_admission(self):
        engine = self.engine()
        for batch in range(4):
            with self.subTest(batch=batch):
                request = self.request()
                barrier = Barrier(6)

                def go(_):
                    with engine.begin() as connection:
                        barrier.wait(timeout=10)
                        return record_intent(connection, request)

                with ThreadPoolExecutor(max_workers=6) as pool:
                    results = list(pool.map(go, range(6)))
                self.assertEqual(sum(result.created for result in results), 1)
                self.assertEqual(len({result.operation_id for result in results}), 1)
                operation_id = results[0].operation_id
                for table in ("safeact_operations", "safeact_audit_events", "safeact_outbox_events"):
                    self.assertEqual(self.api.count(table, sql.SQL("operation_id = %s"), (operation_id,)), 1)

    def test_different_internal_payloads_race_to_one_winner_and_one_conflict(self):
        engine = self.engine()
        request = self.request()
        barrier = Barrier(2)

        def go(amount):
            try:
                with engine.begin() as connection:
                    barrier.wait(timeout=10)
                    return record_intent(connection, replace(request,
                        payload={"charge_id": "synthetic-charge", "amount_minor": amount}))
            except SubmissionConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(go, (500, 501)))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertTrue(winners[0].created)
        self.assertEqual(self.api.count("safeact_operations", sql.SQL("request_key = %s"), (request.request_key,)), 1)
