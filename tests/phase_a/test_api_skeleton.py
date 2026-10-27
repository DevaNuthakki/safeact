"""API skeleton: health, error mapping, headers, and log redaction.

No database: the connection factory is replaced by fakes.
"""

import io
import json
import logging
import os
import tempfile
import unittest
from contextlib import nullcontext
from unittest import mock

from pathlib import Path

import psycopg
from fastapi import Body
from fastapi.testclient import TestClient
from pydantic import BaseModel

from api import logging as api_logging
from api.app import SECURITY_HEADERS, create_app
from api.errors import LIFECYCLE_ERRORS, ApiError, lifecycle_status
from api.settings import Settings
from deploy.demo.bootstrap import read_credentials, write_credentials
from storage import durable_lifecycle as lifecycle


SETTINGS = Settings(
    schema="safeact_demo", role="safeact_demo_api", password="pw",
)


class FakeConnection:

    def __init__(self, row=(1,)):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query):
        row = self.row

        class Cursor:
            def fetchone(self):
                return row

        return Cursor()


def failing_connect():
    raise psycopg.OperationalError("connection to server failed: password=hunter2")


def client(connect=lambda: FakeConnection()):
    app = create_app(SETTINGS, connect=connect)

    class Login(BaseModel):
        username: str
        password: str

    @app.post("/probe/validate")
    def validate(body: Login = Body(...)):
        return {"ok": True}

    @app.get("/probe/raise/{kind}")
    def raise_kind(kind: str):
        if kind == "api":
            raise ApiError(403, "forbidden", "Not allowed.")
        if kind == "boom":
            raise RuntimeError("secret=synthetic-diagnostic-secret internal detail")
        raise getattr(lifecycle, kind)("SQL detail that must not leak")

    return TestClient(app, raise_server_exceptions=False)


class HealthTests(unittest.TestCase):

    def test_shared_lifespans_close_only_after_last_owner_and_can_restart(self):
        owners = [mock.Mock(), mock.Mock()]
        for owner in owners:
            owner.connection.side_effect = lambda: nullcontext(FakeConnection())
        with mock.patch("api.app.ConnectionPool", side_effect=owners) as factory:
            app = create_app(SETTINGS)
            with TestClient(app) as outer:
                self.assertEqual(outer.get("/healthz").status_code, 200)
                factory.assert_not_called()
                with TestClient(app) as inner:
                    self.assertEqual(inner.get("/readyz").status_code, 200)
                owners[0].close.assert_not_called()
                self.assertEqual(outer.get("/readyz").status_code, 200)
            owners[0].close.assert_called_once()
            with TestClient(app) as restarted:
                self.assertEqual(restarted.get("/readyz").status_code, 200)
            owners[1].close.assert_called_once()
            self.assertEqual(factory.call_count, 2)

    def test_healthz_needs_no_database(self):
        response = client(failing_connect).get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_readyz_reports_database_reachability(self):
        self.assertEqual(client().get("/readyz").status_code, 200)

        down = client(failing_connect).get("/readyz")
        self.assertEqual(down.status_code, 503)
        self.assertEqual(down.json()["error"]["code"], "unavailable")
        self.assertNotIn("hunter2", down.text)

        wrong = client(lambda: FakeConnection(row=None)).get("/readyz")
        self.assertEqual(wrong.status_code, 503)


class ErrorMappingTests(unittest.TestCase):

    def test_every_lifecycle_refusal_has_a_stable_status(self):
        refusals = [
            obj for obj in vars(lifecycle).values()
            if isinstance(obj, type)
            and issubclass(obj, lifecycle.LifecycleRejected)
        ]
        self.assertGreater(len(refusals), 10)
        for kind in refusals:
            with self.subTest(kind=kind.__name__):
                status, code, _ = lifecycle_status(kind("x"))
                self.assertIn(status, (404, 409))
                self.assertRegex(code, r"^[a-z_]+$")

    def test_specific_refusals(self):
        expected = {
            "UnknownOperation": (404, "not_found"),
            "StaleTransition": (409, "stale_version"),
            "IllegalTransition": (409, "illegal_transition"),
            "ContractUnknown": (409, "contract_unknown"),
            "RetryNotProtected": (409, "precondition_failed"),
        }
        c = client()
        for name, (status, code) in expected.items():
            with self.subTest(name=name):
                response = c.get(f"/probe/raise/{name}")
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["error"]["code"], code)
                self.assertNotIn("SQL detail", response.text)
                self.assertTrue(response.json()["error"]["request_id"])

    def test_mapping_order_puts_the_base_class_last(self):
        self.assertIs(LIFECYCLE_ERRORS[-1][0], lifecycle.LifecycleRejected)

    def test_api_error_and_unexpected_errors(self):
        c = client()
        refused = c.get("/probe/raise/api")
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(refused.json()["error"]["code"], "forbidden")

        boom = c.get("/probe/raise/boom")
        self.assertEqual(boom.status_code, 500)
        self.assertEqual(boom.json()["error"]["code"], "internal_error")
        self.assertNotIn("synthetic-diagnostic-secret", boom.text)

    def test_validation_errors_never_echo_input(self):
        response = client().post(
            "/probe/validate",
            json={"username": "demo-requester", "password": 12345678},
        )
        self.assertEqual(response.status_code, 422)
        body = response.json()["error"]
        self.assertEqual(body["code"], "invalid_request")
        self.assertEqual(body["fields"][0]["loc"], ["body", "password"])
        self.assertNotIn("12345678", response.text)
        self.assertNotIn("demo-requester", response.text)

    def test_unknown_route_uses_the_error_shape(self):
        response = client().get("/nope")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")


class HeaderTests(unittest.TestCase):

    def test_security_headers_on_every_response(self):
        c = client()
        for path in ("/healthz", "/nope"):
            response = c.get(path)
            for name, value in SECURITY_HEADERS.items():
                self.assertEqual(response.headers[name], value)
            self.assertRegex(response.headers["X-Request-Id"], r"^[0-9a-f]{32}$")

    def test_interactive_docs_are_off_but_schema_is_served(self):
        c = client()
        self.assertEqual(c.get("/docs").status_code, 404)
        self.assertEqual(c.get("/redoc").status_code, 404)
        self.assertEqual(c.get("/openapi.json").status_code, 200)


class SettingsTests(unittest.TestCase):

    def test_public_and_invalid_names_are_refused(self):
        with self.assertRaises(ValueError):
            Settings(schema="public", role="safeact_demo_api", password="x")
        with self.assertRaises(ValueError):
            Settings(schema="safeact_demo2", role="safeact_demo_api", password="x")
        with self.assertRaises(ValueError):
            Settings(schema="safeact_demo", role="postgres", password="x")
        with self.assertRaises(ValueError):
            Settings(schema="safeact_demo", role="safeact_demo_api", password="")

    def test_password_is_not_in_repr(self):
        self.assertNotIn("pw", repr(SETTINGS).replace("password", ""))


class CredentialsFileTests(unittest.TestCase):

    def test_written_mode_600_and_read_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".demo-credentials"
            write_credentials(path, {"SAFEACT_DEMO_SCHEMA": "safeact_demo"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(path.read_text().startswith("# DEMO ONLY"))
            self.assertEqual(
                read_credentials(path), {"SAFEACT_DEMO_SCHEMA": "safeact_demo"}
            )
            os.chmod(path, 0o640)
            with self.assertRaises(RuntimeError):
                read_credentials(path)


class RedactionTests(unittest.TestCase):

    def test_sensitive_keys_and_inline_secrets_are_masked(self):
        cleaned = api_logging.redact({
            "password": "p1",
            "Session_Secret": "s1",
            "X-SafeAct-CSRF": "c1",
            "nested": {"Authorization": "Bearer t1", "ok": "fine"},
            "message": "login password=p2 token: t2 then",
            "dsn": "postgresql://safeact_demo_api:p3@127.0.0.1/db",
            "list": ["secret=p4", 5],
        })
        text = json.dumps(cleaned)
        for secret in ("p1", "s1", "c1", "t1", "p2", "t2", "p3", "p4"):
            self.assertNotIn(secret, text)
        self.assertEqual(cleaned["nested"]["ok"], "fine")
        self.assertIn("127.0.0.1", cleaned["dsn"])

    def test_formatter_emits_redacted_json(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(api_logging.JsonFormatter())
        logger = logging.getLogger("safeact.test.redaction")
        logger.handlers[:] = [handler]
        logger.propagate = False
        logger.warning("connect password=hunter2", extra={
            "csrf": "abc", "request_id": "r1",
        })
        record = json.loads(stream.getvalue())
        self.assertEqual(record["request_id"], "r1")
        self.assertEqual(record["csrf"], api_logging.REDACTED)
        self.assertNotIn("hunter2", stream.getvalue())

    def test_request_log_has_path_but_no_query_string(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(api_logging.JsonFormatter())
        logger = logging.getLogger("safeact.api")
        previous = logger.handlers[:], logger.level, logger.propagate
        logger.handlers[:] = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        try:
            client().get("/healthz?token=leak123")
        finally:
            logger.handlers[:], logger.level, logger.propagate = previous
        self.assertIn('"path": "/healthz"', stream.getvalue())
        self.assertNotIn("leak123", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
