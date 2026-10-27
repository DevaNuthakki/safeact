"""Opt-in PostgreSQL HMAC admission security regression tests."""

import os
import json
import time
import unittest
from secrets import token_bytes,token_hex,token_urlsafe
from dataclasses import replace

import jwt
import psycopg
from psycopg import sql
from psycopg.errors import InsufficientPrivilege,LockNotAvailable
from cryptography.hazmat.primitives.asymmetric import rsa

from core.admission_authorization import RequestedIntent
from core.verified_identity import IdentityVerifier
from storage.atomic_gate_sql import (
    gate_call_sql,gate_signature_sql,
)
from storage.disposable_hmac_proof import (
    ARGUMENT_FIELDS,
    SIGNED_FIELDS,
    issue_verified_proof,
    proof_parameters,
    proof_part_sql,
    protected_gate_sql,
    proof_part_signature,
    proof_gate_signature,
    proof_call_sql,
)
import tests.integration.test_atomic_gate_security as fixture_module
from tests.integration.test_postgres_smoke import database_settings


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1"
    and os.environ.get("SAFEACT_RUN_DB_SECURITY_TESTS") == "1",
    "Disposable HMAC PostgreSQL tests not enabled",
)
class DisposableHmacProofSecurityTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.rsa_key = rsa.generate_private_key(
            public_exponent=65537,key_size=2048
        )
        jwk = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(
                cls.rsa_key.public_key()
            )
        )
        jwk.update(kid="hmac-proof-test",alg="RS256",use="sig")
        cls.verifier = IdentityVerifier(
            issuer="https://id.example",
            audience="safeact-admission",
            public_jwks=(jwk,),
        )

    def setUp(self):
        self.fixture = fixture_module.AtomicGateSecurityTests(
            methodName="test_atomic_commit_and_same_actor_replay"
        )
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.S = self.fixture.S
        self.secret = token_bytes(32)
        admin = self.fixture.admin

        if admin.execute("""
            SELECT count(*) FROM pg_extension
            WHERE extname='pgcrypto'
        """).fetchone()[0] != 1:
            self.fail("pgcrypto must already be installed")

        admin.execute(sql.SQL("""
            CREATE TABLE {}.proof_keys(
                key_id text PRIMARY KEY,
                signing_key bytea NOT NULL,
                enabled boolean NOT NULL DEFAULT FALSE
            )
        """).format(self.S))

        admin.execute(sql.SQL("""
            CREATE TABLE {}.proof_nonces(
                nonce text PRIMARY KEY,
                expires_at bigint NOT NULL
            )
        """).format(self.S))

        admin.execute(sql.SQL(
            "GRANT SELECT,UPDATE ON {}.proof_keys TO {}"
        ).format(self.S,self.fixture.OWNER))

        admin.execute(sql.SQL(
            "GRANT SELECT,INSERT ON {}.proof_nonces TO {}"
        ).format(self.S,self.fixture.OWNER))

        admin.execute(sql.SQL("""
            INSERT INTO {}.proof_keys(key_id,signing_key,enabled)
            VALUES (%s,%s,TRUE)
        """).format(self.S),("test-key-v1",self.secret))

        admin.execute(proof_part_sql(self.fixture.schema))
        admin.execute(sql.SQL(
            "ALTER FUNCTION {} OWNER TO {}"
        ).format(
            proof_part_signature(self.fixture.schema),
            self.fixture.OWNER,
        ))
        admin.execute(sql.SQL(
            "REVOKE ALL ON FUNCTION {} FROM PUBLIC"
        ).format(proof_part_signature(self.fixture.schema)))

        admin.execute(protected_gate_sql(self.fixture.schema))
        signature = proof_gate_signature(self.fixture.schema)

        admin.execute(sql.SQL(
            "ALTER FUNCTION {} OWNER TO {}"
        ).format(signature,self.fixture.OWNER))
        admin.execute(sql.SQL(
            "REVOKE ALL ON FUNCTION {} FROM PUBLIC"
        ).format(signature))
        admin.execute(sql.SQL(
            "GRANT EXECUTE ON FUNCTION {} TO {}"
        ).format(signature,self.fixture.RUNTIME))
        admin.execute(sql.SQL(
            "REVOKE EXECUTE ON FUNCTION {} FROM {}"
        ).format(
            gate_signature_sql(self.fixture.schema),
            self.fixture.RUNTIME,
        ))

        runtime = self.fixture.runtime_name
        old_signature = (
            self.fixture.schema+".admit_real_intent("
            +",".join(("text",)*11)+")"
        )
        protected_signature = (
            self.fixture.schema+".admit_with_proof("
            +",".join(("text",)*12)
            +",bigint,bigint,text,bytea)"
        )

        access = admin.execute("""
            SELECT
              has_function_privilege(%s,%s,'EXECUTE'),
              has_function_privilege(%s,%s,'EXECUTE'),
              has_table_privilege(%s,%s,'SELECT'),
              has_table_privilege(%s,%s,'INSERT')
        """,(
            runtime,old_signature,
            runtime,protected_signature,
            runtime,f"{self.fixture.schema}.proof_keys",
            runtime,f"{self.fixture.schema}.proof_nonces",
        )).fetchone()

        self.assertEqual(access,(False,True,False,False))

    def token(self,subject="subject-a"):
        now = int(time.time())
        return jwt.encode(
            {
                "iss":"https://id.example",
                "aud":"safeact-admission",
                "sub":subject,
                "iat":now-10,
                "nbf":now-10,
                "exp":now+600,
            },
            self.rsa_key,algorithm="RS256",
            headers={"kid":"hmac-proof-test"},
        )

    def request(self,key="proof-request-001"):
        return RequestedIntent(
            request_key=key,
            action_type="create_case",
            resource_id="resource-a",
            provider_account_id="account-a",
            connector_id="connector-a",
            contract_version="v1",
            payload={"case_id":"resource-a"},
        )

    def proof(self,key="proof-request-001",**kwargs):
        return issue_verified_proof(
            identity_verifier=self.verifier,
            token=self.token(),
            request=self.request(key),
            signing_key=self.secret,
            **kwargs,
        )

    def call_with(self,connection,proof):
        connection.execute(
            sql.SQL("SET LOCAL ROLE {}").format(
                self.fixture.RUNTIME
            )
        )
        return connection.execute(
            proof_call_sql(self.fixture.schema),
            proof_parameters(proof),
        ).fetchone()

    def call(self,proof):
        return self.call_with(self.fixture.client,proof)

    def deny(self,proof):
        with self.assertRaises(InsufficientPrivilege):
            self.call(proof)
        self.fixture.client.rollback()

    def test_valid_commit_fresh_replay_and_nonce(self):
        original = self.proof()
        first = self.call(original)
        self.assertTrue(first[1])
        self.assertEqual(self.fixture.counts(),(0,0,0,0))
        self.fixture.client.commit()

        self.assertEqual(self.fixture.counts(),(1,0,1,1))
        self.deny(original)

        fresh = self.proof()
        second = self.call(fresh)
        self.assertEqual(second,(first[0],False))
        self.fixture.client.commit()
        self.assertEqual(self.fixture.counts(),(1,0,1,1))

    def test_all_signed_field_tampering_denied(self):
        baseline = self.proof("tamper-request")
        alterations = {
            "issuer":"https://other.example",
            "subject":"subject-b",
            "request_key":"changed-key",
            "action_type":"delete_case",
            "resource_id":"changed-resource",
            "connector_id":"changed-connector",
            "provider_account_id":"changed-account",
            "contract_version":"v2",
            "payload_canonical":'{"case_id":"other"}',
            "payload_sha256":"0"*64,
            "provider_idempotency_key":"other-provider-key",
            "proof_key_id":"unknown-key",
            "issued_at":baseline["issued_at"]+1,
            "expires_at":baseline["expires_at"]+1,
            "nonce":token_hex(16),
        }
        self.assertEqual(
            set(alterations),set(SIGNED_FIELDS)-{"domain"}
        )
        for field,value in alterations.items():
            with self.subTest(field=field):
                proof = dict(baseline)
                proof[field] = value
                self.deny(proof)
        self.assertEqual(self.fixture.counts(),(0,0,0,0))

    def test_expired_invalid_mac_and_rollback(self):
        now = int(time.time())
        expired = self.proof(
            "expired",
            issued_at=now-90,
            expires_at=now-60,
        )
        self.deny(expired)

        wrong_mac = self.proof("wrong-mac")
        wrong_mac["mac"] = bytes(32)
        self.deny(wrong_mac)

        pending = self.proof("rollback")
        self.assertTrue(self.call(pending)[1])
        self.fixture.client.rollback()
        self.assertEqual(self.fixture.counts(),(0,0,0,0))

        # Nonce insertion rolls back with the operation.
        self.assertTrue(self.call(pending)[1])
        self.fixture.client.commit()
        self.assertEqual(self.fixture.counts(),(1,0,1,1))

    def test_runtime_cannot_read_secret_or_invoke_old_gate(self):
        connection = self.fixture.client
        attempts = (
            (
                sql.SQL("SELECT signing_key FROM {}.proof_keys")
                    .format(self.S),
                None,
            ),
            (
                sql.SQL("SELECT nonce FROM {}.proof_nonces")
                    .format(self.S),
                None,
            ),
            (
                gate_call_sql(self.fixture.schema),
                self.fixture.args(
                    subject="subject-b",
                    key="direct-forgery",
                ),
            ),
            (
                sql.SQL(
                    "INSERT INTO {}.safeact_operations DEFAULT VALUES"
                ).format(self.S),
                None,
            ),
        )
        for statement,params in attempts:
            connection.execute(
                sql.SQL("SET LOCAL ROLE {}").format(
                    self.fixture.RUNTIME
                )
            )
            with self.assertRaises(InsufficientPrivilege):
                connection.execute(statement,params)
            connection.rollback()

        self.assertEqual(self.fixture.counts(),(0,0,0,0))

    def test_revocation_cannot_overtake_proof_admission(self):
        original = self.proof()
        self.assertTrue(self.call(original)[1])
        revoker = psycopg.connect(**database_settings())
        try:
            revoker.execute("SET LOCAL lock_timeout='300ms'")
            with self.assertRaises(LockNotAvailable):
                revoker.execute(sql.SQL("""
                    UPDATE {}.safeact_auth_grants
                    SET enabled=FALSE WHERE actor_id='actor-a'
                """).format(self.S))
            revoker.rollback()
            self.fixture.client.commit()

            revoker.execute(sql.SQL("""
                UPDATE {}.safeact_auth_grants
                SET enabled=FALSE WHERE actor_id='actor-a'
            """).format(self.S))
            revoker.commit()

            self.deny(self.proof("after-revocation"))
            self.assertEqual(self.fixture.counts(),(1,0,1,1))
        finally:
            revoker.rollback()
            revoker.close()

    def test_real_tcp_login_cannot_impersonate_subject(self):
        suffix = self.fixture.schema[len("safeact_it_"):]
        login = "safeact_login_it_"+suffix
        identifier = sql.Identifier(login)
        password = token_urlsafe(48)
        admin = self.fixture.admin

        admin.execute(sql.SQL("""
            CREATE ROLE {}
            LOGIN NOINHERIT NOSUPERUSER
            NOCREATEDB NOCREATEROLE NOREPLICATION
            CONNECTION LIMIT 2 PASSWORD {}
        """).format(identifier,sql.Literal(password)))

        client = None
        def clean_login():
            if client is not None:
                try:
                    client.rollback()
                finally:
                    client.close()
            admin.execute(sql.SQL(
                "DROP ROLE IF EXISTS {}"
            ).format(identifier))

        self.addCleanup(clean_login)

        admin.execute(sql.SQL(
            "GRANT {} TO {}"
        ).format(self.fixture.RUNTIME,identifier))

        settings = {
            **database_settings(),
            "host":"127.0.0.1",
            "user":login,
            "password":password,
            "connect_timeout":5,
        }
        try:
            incorrect = psycopg.connect(**{
                **settings,
                "password":token_urlsafe(48),
            })
        except psycopg.OperationalError as exc:
            self.assertIn(
                "password authentication failed",
                str(exc).lower(),
            )
        else:
            incorrect.close()
            self.fail("Incorrect TCP password was accepted")

        client = psycopg.connect(**settings)
        self.assertEqual(
            client.execute(
                "SELECT session_user,current_user"
            ).fetchone(),
            (login,login),
        )

        # Verify actual TCP credentials cannot bypass proof
        # verification or read the private HMAC material.
        client.execute(
            sql.SQL("SET LOCAL ROLE {}").format(
                self.fixture.RUNTIME
            )
        )
        with self.assertRaises(InsufficientPrivilege):
            client.execute(
                gate_call_sql(self.fixture.schema),
                self.fixture.args(
                    subject="subject-b",
                    key="tcp-direct-bypass",
                ),
            )
        client.rollback()

        client.execute(
            sql.SQL("SET LOCAL ROLE {}").format(
                self.fixture.RUNTIME
            )
        )
        with self.assertRaises(InsufficientPrivilege):
            client.execute(
                sql.SQL(
                    "SELECT signing_key FROM {}.proof_keys"
                ).format(self.S)
            )
        client.rollback()

        signed = self.proof("tcp-valid")
        self.assertTrue(self.call_with(client,signed)[1])
        client.commit()

        tampered = self.proof("tcp-forged")
        tampered["subject"] = "subject-b"

        with self.assertRaises(InsufficientPrivilege):
            self.call_with(client,tampered)
        client.rollback()

        self.assertEqual(self.fixture.counts(),(1,0,1,1))


if __name__ == "__main__":
    unittest.main()
