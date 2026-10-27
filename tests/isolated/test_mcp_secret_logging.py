"""Agent keys never reach the PostgreSQL log (M4.1, migration 0016).

Opt-in (SAFEACT_RUN_ISOLATED_CLUSTER_TESTS=1). Starts a throwaway
PostgreSQL 14.23 cluster with the noisiest logging the server has
(every statement, every parameter, every duration, statements of failed
calls), proven live by a marker that must appear. Builds a complete
lifecycle schema at head, gives the MCP role the no-secret logging
settings, and drives agent calls as that role: a submission, reads, a
refused proposal, and calls with a forged and a revoked key. The server
log must contain none of the keys. A control role without the settings
does leak the key it uses.
"""

import json
import os
import secrets
import unittest
import uuid

from psycopg import sql

from core.connector_profiles import payments_profile
from storage import agent_api as agent
from storage import product_api as product
from storage.identity_admin import (
    create_principal, issue_agent_key, revoke_agent_key,
    role_logging_statements,
)
from storage.lifecycle_environment import (
    contract_insert, contract_params, schema_statements,
)
from tests.isolated.test_api_secret_logging import (
    LOUD_LOGGING, REJECT_MUTATION,
)
from tests.isolated.test_postgresql_identity_isolation import (
    ADMIN,
    DATABASE,
    DisposableCluster,
)


MCP = "safeact_iso_mcp"
LOUD_MCP = "safeact_iso_mcp_loud"
OWNER = "safeact_iso_mcp_owner"
PLACEHOLDERS = ("safeact_iso_mcp_worker", "safeact_iso_mcp_recoverer")
SCHEMA = "safeact_it_" + uuid.uuid4().hex
CHARGE = "ch_iso_0001"


class McpSecretLoggingTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if os.environ.get("SAFEACT_RUN_ISOLATED_CLUSTER_TESTS") != "1":
            raise unittest.SkipTest("Explicit disposable-cluster opt-in required")

        cls.cluster = DisposableCluster()
        cls.addClassCleanup(cls.cluster.cleanup)
        cls.cluster.start()
        cls.role_password = secrets.token_urlsafe(32)
        cls.marker = "safeact-log-marker-" + secrets.token_hex(8)

        with cls.cluster.connect(ADMIN, cls.cluster.bootstrap_password) as admin:
            for name, value in LOUD_LOGGING.items():
                admin.execute(sql.SQL("ALTER SYSTEM SET {} = {}").format(
                    sql.Identifier(name), sql.Literal(value)
                ))
            admin.execute("SELECT pg_reload_conf()")
            admin.execute("CREATE EXTENSION pgcrypto SCHEMA public")
            admin.execute(REJECT_MUTATION)

            for role in (OWNER, *PLACEHOLDERS):
                admin.execute(sql.SQL(
                    "CREATE ROLE {} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(role)))
            for role in (MCP, LOUD_MCP):
                admin.execute(sql.SQL(
                    "CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(role), sql.Literal(cls.role_password)))
                admin.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(DATABASE), sql.Identifier(role)
                ))
            for statement in role_logging_statements(MCP):
                admin.execute(statement)
            admin.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                sql.Identifier(OWNER)
            ))

            with admin.transaction():
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(
                    sql.Identifier(SCHEMA)
                ))
                for statement in schema_statements(
                    SCHEMA, owner=OWNER, workers=PLACEHOLDERS[:1],
                    recoverers=PLACEHOLDERS[1:], advancers=(),
                    mcps=(MCP, LOUD_MCP),
                ):
                    admin.execute(statement)
                admin.execute(
                    contract_insert(SCHEMA), contract_params(payments_profile())
                )
                principal = create_principal(
                    admin, SCHEMA, tenant_id="tenant-iso",
                    username="iso-agent", display_name="Isolated Agent",
                    kind="service", roles=("investigator", "requester"),
                    grants=(("mock-payments", "mock-payments-account",
                             "refund_payment", CHARGE, "v1"),),
                )
                keys = [
                    issue_agent_key(
                        admin, SCHEMA, principal_id=principal,
                        scopes=("investigate", "submit"), label=label,
                        created_by="isolated-test",
                    )
                    for label in ("quiet", "revoked", "loud")
                ]
                revoke_agent_key(admin, SCHEMA, keys[1].key_id)
            cls.key, cls.revoked_key, cls.loud_key = (k.key for k in keys)
            cls.forged_key = "sak_" + secrets.token_hex(32)

            # Proves the loud settings are live: this parameter must be
            # logged.
            admin.execute("SELECT %s::text", (cls.marker,))

        cls.drive_mcp()
        with cls.cluster.connect(LOUD_MCP, cls.role_password) as loud:
            agent.operation(loud, SCHEMA, key=cls.loud_key,
                            operation_id=cls.operation_id)
        cls.log = cls.cluster.log.read_text(encoding="utf-8", errors="replace")

    @classmethod
    def drive_mcp(cls):
        def call(action):
            with cls.cluster.connect(MCP, cls.role_password) as connection:
                connection.autocommit = False
                try:
                    result = action(connection)
                    connection.commit()
                    return result
                except (product.ProductRefused, Exception) as refused:
                    connection.rollback()
                    return refused

        admitted = call(lambda c: agent.submit(
            c, SCHEMA, key=cls.key, intent_ref="iso-refund",
            action_type="refund_payment", resource_id=CHARGE,
            provider_account_id="mock-payments-account",
            connector_id="mock-payments", contract_version="v1",
            payload=json.dumps({"amount_minor": 100, "charge_id": CHARGE},
                               separators=(",", ":"), sort_keys=True),
        ))
        assert isinstance(admitted, agent.AgentAdmission), admitted
        cls.operation_id = admitted.operation_id
        assert isinstance(call(lambda c: agent.operation(
            c, SCHEMA, key=cls.key, operation_id=cls.operation_id,
        )), dict)
        # Refused calls: their statements would carry the key.
        for key in (cls.forged_key, cls.revoked_key):
            assert isinstance(call(lambda c: agent.operation(
                c, SCHEMA, key=key, operation_id=cls.operation_id,
            )), product.Unauthenticated)
        assert isinstance(call(lambda c: agent.propose(
            c, SCHEMA, key=cls.key, operation_id=cls.operation_id,
            action="ATTEST_APPLIED", rationale="no",
        )), product.NotEligible)

    def lines_of(self, role):
        return [
            line for line in self.log.splitlines()
            if f" user={role} " in line
        ]

    def test_logging_was_actually_on(self):
        self.assertIn(self.marker, self.log)
        # Control: without the role settings a key is logged.
        self.assertIn(self.loud_key, self.log)
        # The MCP role's refusals are still visible as errors.
        self.assertIn("Not authenticated", self.log)
        self.assertIn("Not a permitted proposal for an agent", self.log)

    def test_no_agent_key_reaches_the_log(self):
        for name, value in {
            "key": self.key,
            "revoked key": self.revoked_key,
            "forged key": self.forged_key,
        }.items():
            with self.subTest(secret=name):
                self.assertNotIn(value, self.log)
                self.assertNotIn(value[4:], self.log)

    def test_mcp_role_statements_are_not_logged_at_all(self):
        mcp = self.lines_of(MCP)
        self.assertTrue(any("ERROR:" in line for line in mcp))
        for line in mcp:
            for leak in ("statement:", "execute ", "parameters:", "duration:"):
                self.assertNotIn(leak, line)

    def test_control_role_without_settings_logs_its_calls(self):
        loud = self.lines_of(LOUD_MCP)
        self.assertTrue(any("parameters:" in line for line in loud))
        self.assertTrue(any(
            f'"{SCHEMA}"."safeact_mcp_operation"' in line for line in loud
        ))


if __name__ == "__main__":
    unittest.main()
