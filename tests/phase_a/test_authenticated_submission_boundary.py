"""Verify token admission failures never reach storage."""

import json
import time
import unittest
from dataclasses import replace
from unittest.mock import patch, sentinel

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from core.admission_authorization import (
    AdmissionDenied,
    AdmissionGrant,
    RequestedIntent,
    TrustedActorContext,
)
from core.authority_registry import (
    ActorAuthorityRecord,
    AuthorityRegistry,
    AuthorityResolutionDenied,
)
from core.verified_identity import (
    IdentityVerificationError,
    IdentityVerifier,
    VerifiedPrincipal,
)
from storage.authenticated_submission import (
    AuthenticatedAdmissionService,
)


ISSUER = "https://id.safeact.example"
AUDIENCE = "safeact-admission"


class PoisonEngine:
    """Any accidental database access fails the test."""

    def begin(self):
        raise AssertionError("Database accessed before authorization")

    def connect(self):
        raise AssertionError("Database accessed before authorization")


class AuthenticatedSubmissionBoundaryTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        cls.attacker_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        cls.public_jwk = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(
                cls.key.public_key()
            )
        )
        cls.public_jwk.update(
            kid="test-key",
            alg="RS256",
            use="sig",
        )

    def setUp(self):
        self.engine = PoisonEngine()

        self.verifier = IdentityVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            public_jwks=(self.public_jwk,),
        )

        self.grant = AdmissionGrant(
            tenant_id="tenant-alpha",
            actor_id="actor-alpha",
            action_type="create_case",
            resource_id="case-123",
            provider_account_id="account-alpha",
            connector_id="mock-crm",
            contract_version="v1",
        )

        self.record = ActorAuthorityRecord(
            issuer=ISSUER,
            subject="subject-alpha",
            tenant_id="tenant-alpha",
            actor_id="actor-alpha",
            grants=(self.grant,),
        )

        self.request = RequestedIntent(
            request_key="request-123",
            action_type="create_case",
            resource_id="case-123",
            provider_account_id="account-alpha",
            connector_id="mock-crm",
            contract_version="v1",
            payload={"case_id": "case-123"},
        )

        self.service = self.make_service()

    def make_service(self, record=None):
        registry = AuthorityRegistry(
            audience=AUDIENCE,
            records=(
                self.record if record is None else record,
            ),
        )

        return AuthenticatedAdmissionService(
            engine=self.engine,
            identity_verifier=self.verifier,
            authority_registry=registry,
        )

    def sign(self, changes=None, *, key=None, headers=None):
        now = int(time.time())

        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "subject-alpha",
            "iat": now - 20,
            "nbf": now - 20,
            "exp": now + 600,
        }

        if changes:
            claims.update(changes)

        selected_headers = {"kid": "test-key"}

        if headers:
            selected_headers.update(headers)

        return jwt.encode(
            claims,
            self.key if key is None else key,
            algorithm="RS256",
            headers=selected_headers,
        )

    def assert_denied(self, exception, token, request=None):
        proposal = self.request if request is None else request

        with self.assertRaises(exception):
            self.service.submit(
                token=token,
                request=proposal,
            )

        with self.assertRaises(exception):
            self.service.observe(
                token=token,
                request=proposal,
            )

    def test_valid_token_submits_mapped_actor(self):
        with patch(
            "storage.authenticated_submission.submit_scoped_intent",
            return_value=sentinel.receipt,
        ) as submission:
            result = self.service.submit(
                token=self.sign(),
                request=self.request,
            )

        self.assertIs(result, sentinel.receipt)

        submission.assert_called_once_with(
            self.engine,
            TrustedActorContext(
                tenant_id="tenant-alpha",
                actor_id="actor-alpha",
            ),
            (self.grant,),
            self.request,
        )

    def test_valid_token_observes_mapped_actor(self):
        with patch(
            "storage.authenticated_submission.observe_scoped_intent",
            return_value=sentinel.observation,
        ) as observation:
            result = self.service.observe(
                token=self.sign(),
                request=self.request,
            )

        self.assertIs(result, sentinel.observation)

        observation.assert_called_once_with(
            self.engine,
            TrustedActorContext(
                tenant_id="tenant-alpha",
                actor_id="actor-alpha",
            ),
            (self.grant,),
            self.request,
        )

    def test_signed_roles_do_not_override_authority(self):
        token = self.sign({
            "tenant_id": "attacker-tenant",
            "actor_id": "attacker-actor",
            "roles": ["superadmin"],
        })

        with patch(
            "storage.authenticated_submission.submit_scoped_intent",
            return_value=sentinel.receipt,
        ) as submission:
            self.service.submit(
                token=token,
                request=self.request,
            )

        actor = submission.call_args.args[1]

        self.assertEqual(actor.tenant_id, "tenant-alpha")
        self.assertEqual(actor.actor_id, "actor-alpha")

    def test_malformed_token_denied_before_storage(self):
        self.assert_denied(
            IdentityVerificationError,
            "not.a.valid.token",
        )

    def test_attacker_signature_denied_before_storage(self):
        self.assert_denied(
            IdentityVerificationError,
            self.sign(key=self.attacker_key),
        )

    def test_wrong_issuer_denied_before_storage(self):
        self.assert_denied(
            IdentityVerificationError,
            self.sign({"iss": "https://attacker.example"}),
        )

    def test_wrong_audience_denied_before_storage(self):
        self.assert_denied(
            IdentityVerificationError,
            self.sign({"aud": "other-service"}),
        )

    def test_expired_token_denied_before_storage(self):
        now = int(time.time())

        self.assert_denied(
            IdentityVerificationError,
            self.sign({
                "iat": now - 100,
                "nbf": now - 100,
                "exp": now - 1,
            }),
        )

    def test_future_token_denied_before_storage(self):
        self.assert_denied(
            IdentityVerificationError,
            self.sign({"nbf": int(time.time()) + 600}),
        )

    def test_unknown_subject_denied_before_storage(self):
        self.assert_denied(
            AuthorityResolutionDenied,
            self.sign({"sub": "unknown-subject"}),
        )

    def test_disabled_actor_denied_before_storage(self):
        self.service = self.make_service(
            replace(self.record, enabled=False)
        )

        self.assert_denied(
            AuthorityResolutionDenied,
            self.sign(),
        )

    def test_missing_grants_denied_before_storage(self):
        self.service = self.make_service(
            replace(self.record, grants=())
        )

        self.assert_denied(
            AuthorityResolutionDenied,
            self.sign(),
        )

    def test_other_resource_denied_before_storage(self):
        proposal = replace(
            self.request,
            resource_id="case-not-granted",
        )

        self.assert_denied(
            AdmissionDenied,
            self.sign(),
            proposal,
        )

    def test_other_provider_denied_before_storage(self):
        proposal = replace(
            self.request,
            provider_account_id="different-account",
        )

        self.assert_denied(
            AdmissionDenied,
            self.sign(),
            proposal,
        )

    def test_forged_principal_is_not_accepted_as_token(self):
        forged = VerifiedPrincipal(
            issuer=ISSUER,
            subject="subject-alpha",
            audience=AUDIENCE,
            issued_at=100,
            expires_at=200,
        )

        self.assert_denied(
            IdentityVerificationError,
            forged,
        )

    def test_wrong_request_object_denied_before_storage(self):
        self.assert_denied(
            AdmissionDenied,
            self.sign(),
            {"request_key": "request-123"},
        )

    def test_caller_cannot_supply_actor_or_grants(self):
        with self.assertRaises(TypeError):
            self.service.submit(
                token=self.sign(),
                request=self.request,
                actor=TrustedActorContext(
                    tenant_id="attacker",
                    actor_id="attacker",
                ),
                grants=(self.grant,),
            )

    def test_token_supplied_remote_key_url_denied(self):
        self.assert_denied(
            IdentityVerificationError,
            self.sign(
                headers={
                    "jku": "https://attacker.example/keys"
                }
            ),
        )


if __name__ == "__main__":
    unittest.main()
