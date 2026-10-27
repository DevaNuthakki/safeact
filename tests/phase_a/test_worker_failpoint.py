"""The worker's harness-only failpoint never arms outside a test schema.

Runs in a subprocess: importing deploy.stack.worker builds a Celery app
and sets billiard's start method, which must not leak into this process.
"""

import os
import subprocess
import sys
import tempfile
import unittest

from pathlib import Path

from deploy.demo.bootstrap import write_credentials


ROOT = Path(__file__).resolve().parents[2]
# Phase A runs with no services and no .env (as in CI): the worker module
# reads local settings at import, so the subprocess gets throwaway ones.
# Nothing connects; Celery builds its app lazily.
CHECK = (
    "import storage.database as d\n"
    "d._local_settings = lambda: {'SAFEACT_DEV_DB_PORT': '55432',"
    " 'SAFEACT_DEV_DB_PASSWORD': 'phasea', 'SAFEACT_DEV_REDIS_PORT': '56379',"
    " 'SAFEACT_DEV_REDIS_PASSWORD': 'phasea'}\n"
    "import deploy.stack.worker as w\n"
    "try:\n"
    "    hook = w._failpoint()\n"
    "except RuntimeError as error:\n"
    "    print('refused:', error)\n"
    "else:\n"
    "    print('armed' if hook else 'off')\n"
)


def lines(schema):
    return {
        "SAFEACT_DEMO_SCHEMA": schema,
        **{f"SAFEACT_DEMO_{n}_ROLE": f"r_{n.lower()}"
           for n in ("API", "WORKER", "RECOVERER", "ADVANCER", "MCP")},
        **{f"SAFEACT_DEMO_{n}_PASSWORD": "x"
           for n in ("API", "WORKER", "RECOVERER", "ADVANCER", "MCP")},
    }


class WorkerFailpointTests(unittest.TestCase):

    def run_with(self, schema, **env):
        with tempfile.TemporaryDirectory() as tmp:
            credentials = Path(tmp) / "credentials"
            write_credentials(credentials, lines(schema))
            result = subprocess.run(
                [sys.executable, "-c", CHECK], cwd=ROOT, text=True,
                capture_output=True, timeout=60,
                env={**os.environ, "SAFEACT_STACK_CREDENTIALS": str(credentials),
                     **env},
            )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return result.stdout.strip().splitlines()[-1]

    def test_off_unless_asked(self):
        self.assertEqual(self.run_with("safeact_demo"), "off")

    def test_refused_for_the_demo_schema(self):
        self.assertTrue(self.run_with(
            "safeact_demo", SAFEACT_STACK_FAILPOINT="after_claim_commit",
            SAFEACT_STACK_FAILPOINT_MARKER="/tmp/never",
        ).startswith("refused:"))

    def test_refused_without_a_marker(self):
        self.assertTrue(self.run_with(
            "safeact_it_" + "0" * 32,
            SAFEACT_STACK_FAILPOINT="after_claim_commit",
        ).startswith("refused:"))

    def test_armed_only_for_a_disposable_schema(self):
        self.assertEqual(self.run_with(
            "safeact_it_" + "0" * 32,
            SAFEACT_STACK_FAILPOINT="after_claim_commit",
            SAFEACT_STACK_FAILPOINT_MARKER="/tmp/marker",
        ), "armed")


if __name__ == "__main__":
    unittest.main()
