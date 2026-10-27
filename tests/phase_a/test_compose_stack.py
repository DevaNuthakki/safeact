"""Static packaging rules for the Compose demo stack (M5.14); no Docker runs.

The stack itself is exercised by deploy/fresh_clone_check.sh (locally and in
.github/workflows/fresh-clone.yml). These checks keep its safety properties
from drifting in an edit: pinned images, loopback-only publishing of the two
doors, one shared network namespace, no secret in any image.
"""

import re
import tempfile
import unittest

from pathlib import Path
from unittest import mock

from deploy.docker import bootstrap


ROOT = Path(__file__).resolve().parents[2]
STACK = (ROOT / "deploy/compose.stack.yaml").read_text()
DOCKERIGNORE = (ROOT / ".dockerignore").read_text().splitlines()


def services(text):
    """Top-level service blocks of the compose file, by name."""
    body = text.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    blocks = re.split(r"\n(?=  [a-z][a-z-]*:\n)", "\n" + body)
    return {block.strip().split(":", 1)[0]: block for block in blocks if block.strip()}


class ComposeStackTests(unittest.TestCase):

    def test_every_image_and_base_is_pinned_by_digest(self):
        images = re.findall(r"^\s+image:\s*(\S+)", STACK, re.M)
        self.assertTrue(images)
        for image in images:
            self.assertRegex(image, r"@sha256:[0-9a-f]{64}$")
        for name in ("python.Dockerfile", "console.Dockerfile"):
            text = (ROOT / "deploy/docker" / name).read_text()
            bases = re.findall(r"^FROM\s+(\S+)", text, re.M)
            self.assertTrue(bases, name)
            for base in bases:
                self.assertRegex(base, r"@sha256:[0-9a-f]{64}$", name)

    def test_only_the_console_and_mcp_doors_are_published_on_loopback(self):
        published = re.findall(r'^\s+- "([^"]*:\d+)"\s*$', STACK, re.M)
        self.assertEqual(published, [
            "127.0.0.1:${SAFEACT_CONSOLE_PORT:-8080}:8080",
            "127.0.0.1:${SAFEACT_MCP_PORT:-8002}:8002",
        ])
        self.assertEqual(STACK.count("ports:"), 1)
        self.assertIn("listen_addresses=127.0.0.1", STACK)
        self.assertIn("--bind\n      - 127.0.0.1", STACK)

    def test_every_service_shares_the_postgres_network_namespace(self):
        found = services(STACK)
        self.assertIn("console", found)
        for name, block in found.items():
            if name == "postgres":
                self.assertNotIn("network_mode", block)
            elif "<<: *python" not in block:
                self.assertIn('network_mode: "service:postgres"', block, name)
        self.assertIn('network_mode: "service:postgres"', STACK.split("services:")[0])

    def test_services_wait_for_the_one_shot_bootstrap(self):
        anchor = STACK.split("\nservices:\n", 1)[0]
        self.assertIn("condition: service_completed_successfully", anchor)
        self.assertIn('"deploy.docker.bootstrap"', services(STACK)["bootstrap"])
        # Re-running the bootstrap under live services rebuilds their roles.
        script = (ROOT / "deploy/fresh_clone_check.sh").read_text()
        self.assertIn("run --rm --no-deps smoke", script)

    def test_console_responses_vary_by_accept(self):
        # A navigation and the app's fetch share URLs (/actions/<id>); without
        # Vary a browser served the cached HTML to the fetch. A location-level
        # add_header would silently drop the server-level ones in nginx.
        conf = (ROOT / "deploy/docker/nginx.conf").read_text()
        console = conf.split("server {", 2)[1]
        head, locations = console.split("location", 1)
        self.assertIn("add_header Vary Accept always;", head)
        self.assertNotIn("add_header", locations)
        self.assertIn("proxy_buffering off;", conf.split("location = /events", 1)[1])

    def test_secrets_never_enter_an_image(self):
        for pattern in (".env", ".env.*", ".demo-credentials", ".demo-credentials-*",
                        ".demo-stripe.json", "*.key", ".git", "docs", ".venv"):
            self.assertIn(pattern, DOCKERIGNORE)
        self.assertIn("!.env.example", DOCKERIGNORE)
        self.assertNotIn("PASSWORD", (ROOT / "deploy/docker/python.Dockerfile").read_text())
        entry = (ROOT / "deploy/docker/entrypoint.sh").read_text()
        self.assertIn("umask 077", entry)
        self.assertIn("unset SAFEACT_DEV_DB_PASSWORD SAFEACT_DEV_REDIS_PASSWORD", entry)

    def test_bootstrap_runs_the_provisioning_file_as_one_transaction(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "provision.sql"
            path.write_text("\\set ON_ERROR_STOP on\nSELECT 1;\n")
            with mock.patch("deploy.docker.bootstrap.psycopg.connect") as connect, \
                    mock.patch("deploy.docker.bootstrap.local_admin_settings", return_value={}):
                with self.assertRaises(RuntimeError):
                    bootstrap.provision(path)
                connect.assert_not_called()
                path.write_text("DO $$ BEGIN END $$;\nSELECT 1;\n")
                bootstrap.provision(path)
            connection = connect.return_value.__enter__.return_value
            connection.execute.assert_called_once_with(path.read_text())
        # The real file is accepted (no meta-commands) and pins the head.
        text = bootstrap.PROVISION.read_text()
        self.assertFalse(any(line.lstrip().startswith("\\") for line in text.splitlines()))


if __name__ == "__main__":
    unittest.main()
