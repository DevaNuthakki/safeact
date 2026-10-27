"""Signed token to committed PostgreSQL intent tests.

Uses the established disposable-schema integration fixture.
"""

import json
import time
import unittest
from dataclasses import replace

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from core.admission_authorization import (
    AdmissionDenied,
    AdmissionGrant,
    RequestedIntent,
)
from core.authority_registry import (
    ActorAuthorityRecord,
    AuthorityRegistry,
    AuthorityResolutionDenied,
)
from core.verified_identity import (
    IdentityVerificationError,
    IdentityVerifier,
)
from storage.authenticated_submission import (
    AuthenticatedAdmissionService,
)
from storage.submission import (
    IntentObservationStatus,
    SubmissionConflict,
)

from tests.integration import test_submission_commits as fixtures


ISSUER = "https://id.safeact.example"
AUDIENCE = "safeact-admission"


class AuthenticatedAdmissionIntegrationTests(unittest.TestCase):

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
            kid="local-test-key",
            alg="RS256",
            use="sig",
        )

    def setUp(self):
        probe = fixtures.CommittedSubmissionTests(
            methodName=(
                "test_engine_submission_commits_complete_bundle"
            )
        )

        self.addCleanup(probe.doCleanups)
        probe.setUp()

        self.probe = probe
        base = probe.request()

        self.proposal = RequestedIntent(
            request_key=base.request_key,
            action_type=base.action_type,
            resource_id=base.resource_id,
            provider_account_id=base.provider_account_id,
            connector_id=base.connector_id,
            contract_version=base.contract_version,
            payload=base.payload,
            provider_idempotency_key=(
                base.provider_idempotency_key
            ),
        )

        self.grant_a = AdmissionGrant(
            tenant_id=base.tenant_id,
            actor_id=base.actor_id,
            action_type=base.action_type,
            resource_id=base.resource_id,
            provider_account_id=base.provider_account_id,
            connector_id=base.connector_id,
            contract_version=base.contract_version,
        )

        self.record_a = ActorAuthorityRecord(
            issuer=ISSUER,
            subject="subject-a",
            tenant_id=base.tenant_id,
            actor_id=base.actor_id,
            grants=(self.grant_a,),
        )

        self.record_b = replace(
            self.record_a,
            subject="subject-b",
            actor_id=base.actor_id + "-other",
            grants=(
                replace(
                    self.grant_a,
                    actor_id=base.actor_id + "-other",
                ),
            ),
        )

        self.verifier = IdentityVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            public_jwks=(self.public_jwk,),
        )

        self.service = self.make_service(
            (self.record_a, self.record_b)
        )

    def make_service(self, records):
        return AuthenticatedAdmissionService(
            engine=self.probe.runtime_engine,
            identity_verifier=self.verifier,
            authority_registry=AuthorityRegistry(
                audience=AUDIENCE,
                records=records,
            ),
        )

    def token(self, subject="subject-a", *, key=None):
        now = int(time.time())

        return jwt.encode(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "sub": subject,
                "iat": now - 20,
                "nbf": now - 20,
                "exp": now + 600,
            },
            self.key if key is None else key,
            algorithm="RS256",
            headers={"kid": "local-test-key"},
        )

    def test_verified_token_records_observes_and_replays(self):
        token = self.token()

        first = self.service.submit(
            token=token,
            request=self.proposal,
        )

        self.assertTrue(first.created)

        observed = self.service.observe(
            token=token,
            request=self.proposal,
        )

        self.assertEqual(
            observed.status,
            IntentObservationStatus.CONFIRMED_RECORDED,
        )

        self.assertEqual(
            observed.operation_id,
            first.operation_id,
        )

        replay = self.service.submit(
            token=token,
            request=self.proposal,
        )

        self.assertFalse(replay.created)

        self.assertEqual(
            replay.operation_id,
            first.operation_id,
        )

        self.assertEqual(
            self.probe.counts(),
            (1, 1, 1, 0),
        )

    def test_other_verified_actor_cannot_claim_request_key(self):
        original = self.service.submit(
            token=self.token("subject-a"),
            request=self.proposal,
        )

        self.assertTrue(original.created)

        with self.assertRaises(SubmissionConflict):
            self.service.observe(
                token=self.token("subject-b"),
                request=self.proposal,
            )

        with self.assertRaises(SubmissionConflict):
            self.service.submit(
                token=self.token("subject-b"),
                request=self.proposal,
            )

        self.assertEqual(
            self.probe.counts(),
            (1, 1, 1, 0),
        )

    def test_denied_tokens_and_scope_create_no_records(self):
        with self.assertRaises(IdentityVerificationError):
            self.service.submit(
                token=self.token(key=self.attacker_key),
                request=self.proposal,
            )

        with self.assertRaises(AuthorityResolutionDenied):
            self.service.submit(
                token=self.token("unknown-subject"),
                request=self.proposal,
            )

        with self.assertRaises(AdmissionDenied):
            self.service.submit(
                token=self.token(),
                request=replace(
                    self.proposal,
                    resource_id="ungranted-resource",
                ),
            )

        self.assertEqual(
            self.probe.counts(),
            (0, 0, 0, 0),
        )


if __name__ == "__main__":
    unittest.main()
