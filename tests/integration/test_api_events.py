"""Change feed and SSE with durable catch-up (M3, D5).

Requires SAFEACT_RUN_DB_INTEGRATION=1 and SAFEACT_RUN_DISPOSABLE_DB_TESTS=1.
"""

import hashlib
import json
import os
import threading
import time
import unittest
import uuid

import psycopg
from psycopg import sql

from api.cookies import CSRF_HEADER, SESSION_COOKIE
from storage import product_api as product
from storage.database import local_admin_settings
from tests.integration.api_fixture import ApiEnvironment


ENABLED = (
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1"
)

REQUESTER = "demo-north-requester"
INVESTIGATOR = "demo-north-investigator"
SOUTH = "demo-south-requester"


def refund(charge):
    return {
        "request_key": "rk-" + uuid.uuid4().hex[:16],
        "action_type": "refund_payment",
        "provider_account_id": "mock-payments-account",
        "payload": {"charge_id": charge, "amount_minor": 100},
    }


def parse(text):
    """SSE text -> list of (event, id, data) for complete frames."""

    frames = []
    for block in text.split("\n\n"):
        fields = {}
        for line in block.split("\n"):
            if line.startswith(":") or ": " not in line:
                continue
            name, value = line.split(": ", 1)
            fields[name] = value
        if "event" in fields:
            frames.append((
                fields["event"], fields.get("id"),
                json.loads(fields.get("data", "{}")),
            ))
    return frames


@unittest.skipUnless(ENABLED, "PostgreSQL integration tests are opt-in")
class EventCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.api = ApiEnvironment(
            sse_poll_seconds=0.05, sse_heartbeat_seconds=0.2,
            sse_max_seconds=1.0,
        ).start()
        cls.addClassCleanup(cls.api.cleanup)
        cls.north, cls.north_csrf = cls.api.signed_in(REQUESTER)
        cls.investigator, _ = cls.api.signed_in(INVESTIGATOR)
        cls.south, cls.south_csrf = cls.api.signed_in(SOUTH)

    def submit(self, client, csrf, charge):
        response = client.post(
            "/actions", json=refund(charge), headers={CSRF_HEADER: csrf}
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["operation_id"]

    def changes(self, client, after):
        with self.api.connect_api() as api:
            return product.changes(
                api, self.api.schema,
                secret=client.cookies.get(SESSION_COOKIE), after=after,
            )

    def ids(self, result):
        return {c["operation_id"] for c in result["changes"]}


class FeedTests(EventCase):

    def test_no_cursor_returns_only_the_horizon(self):
        result = self.changes(self.investigator, None)
        self.assertTrue(result["reset"])
        self.assertEqual(result["changes"], [])
        self.assertRegex(result["horizon"], r"^[0-9]+$")

    def test_changes_after_a_cursor_are_listed_once(self):
        start = self.changes(self.investigator, None)["horizon"]
        operation_id = self.submit(self.north, self.north_csrf, "ch_north_1001")
        time.sleep(0.05)
        first = self.changes(self.investigator, start)
        self.assertIn(operation_id, self.ids(first))
        entry = next(c for c in first["changes"] if c["operation_id"] == operation_id)
        self.assertIn("INTENT_RECORDED", entry["events"])
        again = self.changes(self.investigator, first["horizon"])
        self.assertNotIn(operation_id, self.ids(again))

    def test_a_late_commit_is_delayed_never_skipped(self):
        start = self.changes(self.investigator, None)["horizon"]
        early = self.submit(self.north, self.north_csrf, "ch_north_1002")

        # A transaction that writes first but commits last.
        slow = psycopg.connect(**local_admin_settings())
        try:
            slow.execute(sql.SQL(
                "INSERT INTO {} (operation_id, event_type, actor_id, details) "
                "VALUES (%s, 'TEST_LATE_EVENT', 'test', '{{}}'::jsonb)"
            ).format(self.api.table("safeact_audit_events")), (early,))
            late = self.submit(self.north, self.north_csrf, "ch_north_1003")

            seen = set()
            middle = self.changes(self.investigator, start)
            seen |= self.ids(middle)
            # Nothing at or after the open transaction is reported yet.
            self.assertNotIn(late, seen)
            slow.commit()
        finally:
            slow.close()

        final = self.changes(self.investigator, middle["horizon"])
        seen |= self.ids(final)
        self.assertTrue({early, late} <= seen)
        late_events = next(
            c["events"] for c in middle["changes"] + final["changes"]
            if c["operation_id"] == early and "TEST_LATE_EVENT" in c["events"]
        )
        self.assertIn("TEST_LATE_EVENT", late_events)

    def test_tenants_and_requesters_see_only_what_they_may_read(self):
        start = self.changes(self.investigator, None)["horizon"]
        north = self.submit(self.north, self.north_csrf, "ch_north_1004")
        south = self.submit(self.south, self.south_csrf, "ch_south_1001")
        time.sleep(0.05)
        self.assertIn(north, self.ids(self.changes(self.investigator, start)))
        self.assertNotIn(south, self.ids(self.changes(self.investigator, start)))
        self.assertEqual(self.ids(self.changes(self.south, start)), {south})

    def test_bad_cursors_are_refused(self):
        for after in ("abc", "-1", "9" * 21):
            with self.subTest(after=after):
                response = self.investigator.get("/events", params={"after": after})
                self.assertEqual(response.status_code, 422)
        future = str(int(self.changes(self.investigator, None)["horizon"]) + 10**6)
        response = self.investigator.get("/events", headers={"Last-Event-ID": future})
        self.assertEqual(response.status_code, 422)

    def test_unauthenticated_streams_never_open(self):
        response = self.api.client().get("/events")
        self.assertEqual(response.status_code, 401)


class StreamTests(EventCase):

    def test_reconnect_with_last_event_id_catches_up(self):
        start = self.changes(self.investigator, None)["horizon"]
        missed = self.submit(self.north, self.north_csrf, "ch_north_1001")
        time.sleep(0.05)
        response = self.investigator.get(
            "/events", headers={"Last-Event-ID": start}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers["content-type"].startswith("text/event-stream")
        )
        frames = parse(response.text)
        changed = [f for f in frames if f[0] == "changes"]
        self.assertTrue(any(
            missed in {c["operation_id"] for c in f[2]["changes"]}
            for f in changed
        ))
        self.assertTrue(all(f[1] is not None for f in changed))
        # The stream ends by asking the client to reconnect from its id.
        self.assertEqual(frames[-1][0], "reconnect")
        self.assertIn(": keep-alive", response.text)

    def test_without_a_cursor_the_stream_starts_with_reset(self):
        frames = parse(self.investigator.get("/events").text)
        self.assertEqual(frames[0][0], "reset")
        self.assertRegex(frames[0][1], r"^[0-9]+$")

    def test_revocation_ends_the_stream(self):
        client, csrf = self.api.signed_in(INVESTIGATOR)
        holder = {}

        def watch():
            holder["text"] = client.get("/events").text

        thread = threading.Thread(target=watch)
        thread.start()
        time.sleep(0.2)
        self.api.client(cookies={SESSION_COOKIE: client.cookies.get(SESSION_COOKIE)}).post(
            "/auth/logout", headers={CSRF_HEADER: csrf}
        )
        thread.join(timeout=5)
        frames = parse(holder["text"])
        self.assertEqual(frames[-1][0], "session_ended")

    def test_one_session_may_hold_only_a_few_streams(self):
        client, _ = self.api.signed_in(INVESTIGATOR)
        key = hashlib.sha256(client.cookies.get(SESSION_COOKIE).encode()).hexdigest()
        limiter = self.api.app.state.stream_limiter
        for _ in range(self.api.settings.sse_max_streams):
            self.assertTrue(limiter.acquire(key, self.api.settings.sse_max_streams))
        try:
            response = client.get("/events")
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.json()["error"]["code"], "too_many_streams")
        finally:
            for _ in range(self.api.settings.sse_max_streams):
                limiter.release(key)
        self.assertEqual(client.get("/events").status_code, 200)

    def test_after_a_dropped_stream_rest_shows_the_durable_state(self):
        operation_id = self.submit(self.north, self.north_csrf, "ch_north_1002")
        self.north.get("/events")
        detail = self.north.get(f"/actions/{operation_id}").json()
        state = self.api.admin.execute(sql.SQL(
            "SELECT state, seq FROM {} WHERE operation_id = %s"
        ).format(self.api.table("safeact_operation_current_state")),
            (operation_id,)).fetchone()
        self.assertEqual((detail["state"], detail["seq"]), state)


if __name__ == "__main__":
    unittest.main()
