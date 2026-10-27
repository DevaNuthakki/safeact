"""The demo walkthrough seed (deploy/demo/walkthrough.py) over real HTTP and MCP.

Opt-in: SAFEACT_RUN_API_E2E=1. One disposable environment with e2e-scaled
contracts, the synthetic demo accounts and their agent keys; the in-process
stack (API, MCP, relay, Celery worker on Redis database 14, reconciler,
advancer) with the demo's fixed fault script. The seed runs exactly as the
command does, then the permitted human path is completed through the API.
"""

import os
import socket
import time
import unittest
import uuid

import psycopg
import redis

from deploy.demo import walkthrough
from deploy.demo.bootstrap import (
    DEMO_ACCOUNTS, Environment, issue_agent_keys, rebuild, teardown_statements,
)
from deploy.demo.profiles import E2E_PROFILES
from deploy.demo.run_stack import Stack
from storage.database import local_admin_settings, local_redis_url
from storage.state_projection import projection_drift


ENABLED = os.environ.get("SAFEACT_RUN_API_E2E") == "1"
REDIS_DB = 14


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@unittest.skipUnless(ENABLED, "API end-to-end tests are opt-in")
class WalkthroughSeedTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.redis = redis.Redis.from_url(local_redis_url(REDIS_DB))
        cls.redis.flushdb()
        cls.addClassCleanup(cls.redis.flushdb)
        cls.admin = psycopg.connect(**local_admin_settings(), autocommit=True)
        cls.env = Environment.disposable(uuid.uuid4().hex)
        cls.addClassCleanup(cls.teardown_env)
        role_passwords, passwords = rebuild(
            cls.admin, cls.env, profiles=E2E_PROFILES, accounts=DEMO_ACCOUNTS)
        cls.values = {**passwords, **issue_agent_keys(cls.admin, cls.env)}
        roles = {"api": cls.env.api, "worker": cls.env.worker,
                 "recoverer": cls.env.recoverer, "advancer": cls.env.advancer,
                 "mcp": cls.env.mcp}
        cls.stack = Stack(
            schema=cls.env.schema, roles=roles, passwords=role_passwords,
            port=free_port(), redis_db=REDIS_DB, poll_seconds=0.2,
            mcp_port=free_port(),
        ).start()
        cls.addClassCleanup(cls.stack.stop)

    @classmethod
    def teardown_env(cls):
        drift = projection_drift(cls.admin, cls.env.schema)
        cls.admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE usename = ANY(%s)", (list(cls.env.roles),))
        with cls.admin.transaction():
            for statement in teardown_statements(cls.admin, cls.env):
                cls.admin.execute(statement)
        cls.admin.close()
        if drift:
            raise AssertionError(f"Current-state projection drift: {drift[:3]}")

    def test_seed_sets_up_every_beat_and_the_permitted_path_completes(self):
        result = walkthrough.seed(self.stack.base_url, self.stack.mcp_url, self.values,
                                  stale=True, timeout=90)

        first = result["1_timeout_after_success"]
        self.assertEqual(first["state"], "SUCCEEDED")
        self.assertEqual(first["attempts"], 1)
        self.assertIn("UNKNOWN", first["path"])

        second = result["2_late_commit_empty_lookup"]
        self.assertEqual(second["state"], "ESCALATED_UNRESOLVED")
        self.assertEqual(second["attempts"], 1)

        agent = result["3_agent"]
        self.assertFalse(agent["approval_tool_offered"])
        self.assertNotIn("approve", " ".join(agent["tools"]))
        self.assertTrue(agent["attest_applied_refused"])

        review = result["4_human_review"]
        self.assertEqual(review["stale_approval"]["http_status"], 409)
        self.assertEqual(review["stale_approval"]["code"], "stale_approval")

        # The permitted path: a different human reviewer approves the email's
        # attestation; it succeeds with human provenance and one effect.
        reviewer = walkthrough.Person(self.stack.base_url, walkthrough.REVIEWER,
                                      self.values[walkthrough.REVIEWER])
        self.addCleanup(reviewer.close)
        proposal_id = review["approve_this"].rsplit("/", 1)[1]
        proposal = reviewer.get(f"/proposals/{proposal_id}")
        decided = reviewer.post(f"/proposals/{proposal_id}/decision", {
            "decision": "approve", "binding_digest": proposal["binding_digest"],
            "decision_key": str(uuid.uuid4())})
        self.assertEqual(decided.status_code, 200, decided.text)
        email = agent["email_operation_id"]
        detail = walkthrough.wait_state(reviewer, email, "SUCCEEDED", 60)
        self.assertEqual(detail["state"], "SUCCEEDED")
        self.assertEqual(detail["success_provenance"], "human_attested")
        self.assertEqual(len(detail["attempts"]), 1)
        # Effects from the independent ledger: one each, never a second send.
        key = lambda operation: self.admin.execute(
            f'SELECT provider_idempotency_key FROM "{self.env.schema}".safeact_operations '
            "WHERE operation_id = %s", (operation,)).fetchone()[0]
        refund = first["operation"].rsplit("/", 1)[1]
        order = second["operation"].rsplit("/", 1)[1]
        for operation in (refund, order, email):
            with self.subTest(operation=operation):
                self.assertEqual(self.stack.effects(operation, key(operation)), 1)
        self.assertNotEqual(walkthrough.wait_state(reviewer, order, "SUCCEEDED", 0)["state"],
                            "SUCCEEDED")


if __name__ == "__main__":
    unittest.main()
