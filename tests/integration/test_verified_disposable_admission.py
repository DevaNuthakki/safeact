"""Signed-JWT integration with the disposable atomic gate.

Uses an existing security fixture by composition, avoiding
changes to committed tests or duplicate test discovery.
"""

import json
import os
import time
import unittest
from dataclasses import replace
from hashlib import sha256

import jwt
import psycopg
from psycopg import sql
from psycopg.errors import (
    InsufficientPrivilege,
    LockNotAvailable,
)
from cryptography.hazmat.primitives.asymmetric import rsa

from core.admission_authorization import RequestedIntent
from core.verified_identity import (
    IdentityVerificationError,
    IdentityVerifier,
    VerifiedPrincipal,
)
from storage.submission import (
    SubmissionReceipt,
    SubmissionValidationError,
    _prepare_payload,
)
from storage.verified_disposable_admission import (
    VerifiedDisposableAdmission,
)

import tests.integration.test_atomic_gate_security as fixture_module


ISSUER = "https://id.example"
AUDIENCE = "safeact-admission"


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1"
    and os.environ.get("SAFEACT_RUN_DB_SECURITY_TESTS") == "1",
    "Disposable PostgreSQL signed-token tests not enabled",
)
class VerifiedDisposableAdmissionTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        cls.other_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        jwk = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(
                cls.key.public_key()
            )
        )
        jwk.update(
            kid="verified-test-key",
            alg="RS256",
            use="sig",
        )
        cls.jwk = jwk

    def setUp(self):
        self.fixture = fixture_module.AtomicGateSecurityTests(
            methodName="test_atomic_commit_and_same_actor_replay"
        )

        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

        self.verifier = IdentityVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            public_jwks=(self.jwk,),
        )

        self.gateway = VerifiedDisposableAdmission(
            identity_verifier=self.verifier,
            schema=self.fixture.schema,
        )

        self.request = RequestedIntent(
            request_key="req-001",
            action_type="create_case",
            resource_id="resource-a",
            provider_account_id="account-a",
            connector_id="connector-a",
            contract_version="v1",
            payload={"case_id": "resource-a"},
        )

    def token(
        self,
        subject="subject-a",
        *,
        changes=None,
        key=None,
    ):
        now = int(time.time())

        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": subject,
            "iat": now - 20,
            "nbf": now - 20,
            "exp": now + 600,
        }

        if changes:
            claims.update(changes)

        return jwt.encode(
            claims,
            self.key if key is None else key,
            algorithm="RS256",
            headers={"kid": "verified-test-key"},
        )

    def enter_runtime_role(self):
        self.fixture.client.execute(
            sql.SQL("SET LOCAL ROLE {}").format(
                self.fixture.RUNTIME
            )
        )

    def submit(self, *, token=None, request=None):
        return self.gateway.submit(
            token=self.token() if token is None else token,
            request=self.request if request is None else request,
            connection=self.fixture.client,
        )

    def test_signed_claims_cannot_override_database_actor(self):
        self.enter_runtime_role()

        signed = self.token(changes={
            "tenant_id": "attacker-tenant",
            "actor_id": "attacker-actor",
            "roles": ["superadmin"],
        })

        receipt = self.submit(token=signed)

        self.assertIsInstance(receipt, SubmissionReceipt)
        self.assertTrue(receipt.created)

        self.assertEqual(
            self.fixture.counts(),
            (0, 0, 0, 0),
        )

        self.fixture.client.commit()

        self.assertEqual(
            self.fixture.counts(),
            (1, 0, 1, 1),
        )

        actor = self.fixture.admin.execute(
            sql.SQL("""
                SELECT actor_id
                FROM {}.safeact_audit_events
                WHERE operation_id=%s
                  AND event_type='INTENT_RECORDED'
            """).format(self.fixture.S),
            (receipt.operation_id,),
        ).fetchone()[0]

        self.assertEqual(actor, "actor-a")

    def test_bad_signature_expiry_and_audience_before_sql(self):
        now = int(time.time())

        invalid = (
            self.token(key=self.other_key),
            self.token(changes={
                "iat": now - 100,
                "nbf": now - 100,
                "exp": now - 1,
            }),
            self.token(changes={"aud": "wrong-audience"}),
        )

        for token in invalid:
            with self.subTest(token_kind=token[-8:]):
                with self.assertRaises(IdentityVerificationError):
                    self.submit(token=token)

                self.assertEqual(
                    self.fixture.counts(),
                    (0, 0, 0, 0),
                )

    def test_forged_principal_object_is_rejected(self):
        forged = VerifiedPrincipal(
            issuer=ISSUER,
            subject="subject-a",
            audience=AUDIENCE,
            issued_at=100,
            expires_at=200,
        )

        with self.assertRaises(IdentityVerificationError):
            self.submit(token=forged)

        self.assertEqual(self.fixture.counts(), (0, 0, 0, 0))

    def test_unknown_subject_is_denied(self):
        self.enter_runtime_role()

        with self.assertRaises(InsufficientPrivilege):
            self.submit(token=self.token("unknown-subject"))

        self.fixture.client.rollback()

        self.assertEqual(self.fixture.counts(), (0, 0, 0, 0))

    def test_disabled_identity_and_actor_are_denied(self):
        self.fixture.admin.execute(
            sql.SQL("""
                UPDATE {}.safeact_auth_identities
                SET enabled=FALSE
                WHERE subject='subject-a'
            """).format(self.fixture.S)
        )

        self.enter_runtime_role()

        with self.assertRaises(InsufficientPrivilege):
            self.submit()

        self.fixture.client.rollback()

        self.fixture.admin.execute(
            sql.SQL("""
                UPDATE {}.safeact_auth_identities
                SET enabled=TRUE
                WHERE subject='subject-a'
            """).format(self.fixture.S)
        )

        self.fixture.admin.execute(
            sql.SQL("""
                UPDATE {}.safeact_auth_actors
                SET enabled=FALSE
                WHERE actor_id='actor-a'
            """).format(self.fixture.S)
        )

        self.enter_runtime_role()

        with self.assertRaises(InsufficientPrivilege):
            self.submit()

        self.fixture.client.rollback()

        self.assertEqual(self.fixture.counts(), (0, 0, 0, 0))

    def test_disabled_grant_denies_signed_token(self):
        self.fixture.admin.execute(
            sql.SQL("""
                UPDATE {}.safeact_auth_grants
                SET enabled=FALSE
                WHERE actor_id='actor-a'
            """).format(self.fixture.S)
        )

        self.enter_runtime_role()

        with self.assertRaises(InsufficientPrivilege):
            self.submit()

        self.fixture.client.rollback()

        self.assertEqual(self.fixture.counts(), (0, 0, 0, 0))

    def test_wrong_resource_and_action_denied(self):
        for changed in (
            replace(
                self.request,
                resource_id="wrong-resource",
            ),
            replace(
                self.request,
                action_type="delete_case",
            ),
        ):
            self.enter_runtime_role()

            with self.assertRaises(InsufficientPrivilege):
                self.submit(request=changed)

            self.fixture.client.rollback()

        self.assertEqual(self.fixture.counts(), (0, 0, 0, 0))

    def test_replay_is_idempotent_and_other_actor_denied(self):
        self.enter_runtime_role()

        first = self.submit()
        self.assertTrue(first.created)
        self.fixture.client.commit()

        self.enter_runtime_role()

        replay = self.submit()
        self.assertFalse(replay.created)
        self.assertEqual(replay.operation_id, first.operation_id)

        self.fixture.client.commit()

        self.enter_runtime_role()

        with self.assertRaises(InsufficientPrivilege):
            self.submit(token=self.token("subject-b"))

        self.fixture.client.rollback()

        self.assertEqual(self.fixture.counts(), (1, 0, 1, 1))

    def test_canonical_payload_and_invalid_json(self):
        altered = replace(
            self.request,
            payload={"z": 2, "a": 1},
        )

        wire, digest = _prepare_payload(altered.payload)

        self.enter_runtime_role()

        receipt = self.submit(request=altered)
        self.fixture.client.commit()

        stored = self.fixture.admin.execute(
            sql.SQL("""
                SELECT payload_canonical, payload_sha256
                FROM {}.safeact_operations
                WHERE operation_id=%s
            """).format(self.fixture.S),
            (receipt.operation_id,),
        ).fetchone()

        self.assertEqual(stored, (wire, digest))

        invalid = replace(
            self.request,
            request_key="invalid-payload",
            payload={"object": object()},
        )

        with self.assertRaises(SubmissionValidationError):
            self.submit(request=invalid)

        self.assertEqual(self.fixture.counts(), (1, 0, 1, 1))

    def test_outer_transaction_is_required(self):
        with self.assertRaises(RuntimeError):
            self.submit()

        self.assertEqual(self.fixture.counts(), (0, 0, 0, 0))

    def test_rollback_removes_entire_bundle(self):
        self.enter_runtime_role()

        receipt = self.submit()
        self.assertTrue(receipt.created)

        self.fixture.client.rollback()

        self.assertEqual(self.fixture.counts(), (0, 0, 0, 0))

    def test_signed_admission_blocks_revocation_until_commit(self):
        self.enter_runtime_role()
        self.assertTrue(self.submit().created)

        revoker = psycopg.connect(
            **fixture_module.database_settings()
        )

        try:
            revoker.execute(
                "SET LOCAL lock_timeout='300ms'"
            )

            with self.assertRaises(LockNotAvailable):
                revoker.execute(
                    sql.SQL("""
                        UPDATE {}.safeact_auth_grants
                        SET enabled=FALSE
                        WHERE actor_id='actor-a'
                    """).format(self.fixture.S)
                )

            revoker.rollback()
            self.fixture.client.commit()

            revoker.execute(
                sql.SQL("""
                    UPDATE {}.safeact_auth_grants
                    SET enabled=FALSE
                    WHERE actor_id='actor-a'
                """).format(self.fixture.S)
            )

            revoker.commit()

            self.enter_runtime_role()

            with self.assertRaises(InsufficientPrivilege):
                self.submit(
                    request=replace(
                        self.request,
                        request_key="after-revocation",
                    )
                )

            self.fixture.client.rollback()

            self.assertEqual(
                self.fixture.counts(),
                (1, 0, 1, 1),
            )

        finally:
            revoker.rollback()
            revoker.close()


    def test_unexpected_database_execution_role_is_denied(self):
        # An active transaction alone must not be sufficient.
        # This tests role misconfiguration, not credential isolation.
        self.fixture.client.execute("SELECT 1")

        with self.assertRaises(PermissionError):
            self.submit()

        self.fixture.client.rollback()

        self.assertEqual(
            self.fixture.counts(),
            (0, 0, 0, 0),
        )

if __name__ == "__main__":
    unittest.main()
