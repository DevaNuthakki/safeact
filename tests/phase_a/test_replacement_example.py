"""The integration example preserves identity and stops on uncertainty."""

import argparse
import json
import unittest

import httpx2

from examples.replacement import follow, local_url, sign_in


REQUEST = {"request_key": "example-request-001", "customer_id": "cust_north_3001",
           "case_id": "case_north_2001", "sku": "sku-demo", "fee_minor": 500,
           "payments_account_id": "mock-payments-account", "crm_account_id": "mock-crm-account"}


class ReplacementExampleTests(unittest.TestCase):
    def client(self, handler):
        client = httpx2.Client(base_url="http://127.0.0.1:8000", transport=httpx2.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def test_repeated_admission_keeps_key_and_stops_at_unresolved(self):
        calls = []

        def handle(request):
            calls.append(request)
            if request.method == "POST":
                self.assertEqual(json.loads(request.content), REQUEST)
                return httpx2.Response(200, json={"workflow_id": "wf-original", "created": False})
            return httpx2.Response(200, json={"status": "UNRESOLVED", "steps": []})

        result = follow(self.client(handle), REQUEST, sleep=lambda _: self.fail("must stop on unresolved"))
        self.assertEqual(result["workflow_id"], "wf-original")
        self.assertIn("must not trigger a refund", result["message"])
        self.assertEqual([r.method for r in calls], ["POST", "GET"])

    def test_wait_budget_does_not_submit_again_or_claim_completion(self):
        calls = []

        def handle(request):
            calls.append(request)
            return httpx2.Response(201, json={"workflow_id": "wf-original"}) if request.method == "POST" else (
                httpx2.Response(200, json={"status": "IN_PROGRESS", "steps": []}))

        result = follow(self.client(handle), REQUEST, wait_seconds=0)
        self.assertEqual(result["status"], "IN_PROGRESS")
        self.assertIn("do not submit a new intent", result["message"])
        self.assertEqual(len(calls), 2)

    def test_lost_admission_reply_is_not_automatically_retried(self):
        calls = []

        def handle(request):
            calls.append(request)
            raise httpx2.ReadTimeout("reply lost", request=request)

        with self.assertRaises(httpx2.TransportError):
            follow(self.client(handle), REQUEST)
        self.assertEqual(len(calls), 1)

    def test_conflict_does_not_create_a_new_identity(self):
        calls = []

        def handle(request):
            calls.append(request)
            return httpx2.Response(409, json={"error": {"code": "request_conflict"}})

        with self.assertRaises(httpx2.HTTPStatusError):
            follow(self.client(handle), REQUEST)
        self.assertEqual(len(calls), 1)

    def test_refund_is_reported_as_recovery_not_order_success(self):
        def handle(request):
            return httpx2.Response(200, json={"workflow_id": "wf-original"}) if request.method == "POST" else (
                httpx2.Response(200, json={"status": "COMPENSATED_VERIFIED", "steps": []}))
        result = follow(self.client(handle), REQUEST)
        self.assertIn("replacement not completed", result["message"])

    def test_only_loopback_origins_are_accepted(self):
        for value in ("https://example.com", "http://127.0.0.1.evil", "http://u:p@localhost",
                      "http://localhost/path", "http://localhost?redirect=evil"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                local_url(value)
        self.assertEqual(local_url("http://127.0.0.1:8000/"), "http://127.0.0.1:8000")

    def test_session_is_carried_to_local_api_without_logging_secrets(self):
        seen = []

        def handle(request):
            if request.url.path == "/auth/login":
                return httpx2.Response(200, json={"principal": {"demo": True}, "csrf_token": "demo-csrf"},
                    headers=[("Set-Cookie", "__Host-safeact_session=demo-session; Secure; Path=/"),
                             ("Set-Cookie", "__Host-safeact_csrf=demo-csrf; Secure; Path=/")])
            seen.append(request)
            return httpx2.Response(200)

        client = self.client(handle)
        sign_in(client, "demo-north-requester", "demo-password")
        client.get("/auth/me")
        self.assertIn("demo-session", seen[0].headers["cookie"])
        self.assertEqual(seen[0].headers["X-SafeAct-CSRF"], "demo-csrf")


if __name__ == "__main__":
    unittest.main()
