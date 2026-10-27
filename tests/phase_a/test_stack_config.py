"""Configuration for the separate-process stack (deploy/stack/config.py)."""

import os
import tempfile
import unittest

from pathlib import Path
from unittest import mock

from deploy.demo.bootstrap import write_credentials
from deploy.stack import config
from deploy.stack.processes import ProcessStack


LINES = {
    "SAFEACT_DEMO_SCHEMA": "safeact_demo",
    **{f"SAFEACT_DEMO_{n}_ROLE": f"safeact_demo_{n.lower()}"
       for n in ("API", "WORKER", "RECOVERER", "ADVANCER", "MCP")},
    **{f"SAFEACT_DEMO_{n}_PASSWORD": f"secret-{n.lower()}-value"
       for n in ("API", "WORKER", "RECOVERER", "ADVANCER", "MCP")},
}


class StackConfigTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "credentials"
        write_credentials(self.path, LINES)

    def load(self, **env):
        with mock.patch.dict(os.environ, env):
            return config.load(self.path)

    def test_roles_and_passwords_come_from_the_file(self):
        loaded = self.load()
        self.assertEqual(loaded.schema, "safeact_demo")
        self.assertEqual(loaded.roles["worker"], "safeact_demo_worker")
        self.assertEqual(loaded.passwords["safeact_demo_worker"],
                         "secret-worker-value")
        self.assertNotIn("secret", repr(loaded))

    def test_defaults(self):
        loaded = self.load()
        self.assertEqual(loaded.provider_urls["payments"],
                         "http://127.0.0.1:8101")
        self.assertEqual((loaded.redis_db, loaded.queue, loaded.lease_seconds),
                         (1, "safeact-stack", 30))

    def test_provider_urls_must_be_plain_http_without_credentials(self):
        for url in ("https://127.0.0.1:8101", "http://u:p@127.0.0.1:1",
                    "http://127.0.0.1:1/path", "http://127.0.0.1:1/?q=1",
                    "file:///etc/passwd", "http://"):
            with self.subTest(url=url):
                with self.assertRaises(RuntimeError):
                    self.load(SAFEACT_STACK_PAYMENTS_URL=url)
        self.assertEqual(
            self.load(SAFEACT_STACK_CRM_URL="http://toxiproxy:18102/")
            .provider_urls["crm"], "http://toxiproxy:18102")

    def test_numbers_and_queue_are_checked(self):
        self.assertEqual(self.load(SAFEACT_STACK_REDIS_DB="0").redis_db, 0)
        for env in ({"SAFEACT_STACK_REDIS_DB": "-1"},
                    {"SAFEACT_STACK_LEASE_SECONDS": "0"},
                    {"SAFEACT_STACK_QUEUE": "bad queue;"}):
            with self.subTest(env=env):
                with self.assertRaises((RuntimeError, ValueError)):
                    self.load(**env)

    def test_an_unsafe_credentials_file_is_refused(self):
        self.path.chmod(0o644)
        with self.assertRaises(RuntimeError):
            config.load(self.path)


class ProcessStackCommandTests(unittest.TestCase):

    def test_commands_and_environment(self):
        stack = ProcessStack(credentials="/tmp/c", redis_db=11, queue="q-1",
                             api_port=9000, provider_ports={"payments": 9101},
                             plain_providers=True)
        worker = stack.command("worker")
        self.assertEqual(worker[1:4], ["-A", "deploy.stack.worker", "worker"])
        self.assertIn("--pool=prefork", worker)
        self.assertIn("--plain", stack.command("providers"))
        self.assertIn("9101", stack.command("providers"))
        self.assertEqual(stack.env["SAFEACT_STACK_PAYMENTS_URL"],
                         "http://127.0.0.1:9101")
        self.assertEqual(stack.env["SAFEACT_STACK_CREDENTIALS"], "/tmp/c")
        self.assertNotIn("PASSWORD", " ".join(
            k for k in stack.env if k.startswith("SAFEACT_STACK")))
        with self.assertRaises(ValueError):
            ProcessStack(components=("worker", "shell"))


if __name__ == "__main__":
    unittest.main()
