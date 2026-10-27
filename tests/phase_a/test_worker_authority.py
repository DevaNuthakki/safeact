"""Negative-focused worker scope review regression tests.

These tests use independently signed RS256 JWTs.

No PostgreSQL, provider, worker, or dispatch interface
is accessed by these tests.
"""

import json
import time
import unittest

from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import jwt

from cryptography.hazmat.primitives.asymmetric import rsa

from core.verified_identity import (
    IdentityVerificationError,
    IdentityVerifier,
)
from core.worker_authority import (
    WORKER_AUDIENCE,
    WorkerAccessDenied,
    WorkerAssignment,
    WorkerAuthorityConfigurationError,
    WorkerScopeRequest,
    WorkerScopeReviewService,
)


ISSUER = "https://workers.safeact.example"
ADMISSION_AUDIENCE = "safeact-admission"

# Distinguish an omitted argument from explicit None.
_UNSET = object()


class WorkerAuthorityTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.signing_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        cls.attacker_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        cls.public_jwk = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(
                cls.signing_key.public_key()
            )
        )

        cls.public_jwk.update(
            kid="safeact-worker-review-test-key",
            alg="RS256",
            use="sig",
        )

    def setUp(self):
        self.operation_id = uuid4()

        self.assignment = WorkerAssignment(
            subject="worker-alpha",
            operation_id=self.operation_id,
            tenant_id="tenant-alpha",
            connector_id="crm-alpha",
            provider_account_id="provider-alpha",
            enabled=True,
        )

        self.request = WorkerScopeRequest(
            operation_id=self.operation_id,
            tenant_id="tenant-alpha",
            connector_id="crm-alpha",
            provider_account_id="provider-alpha",
        )

        self.verifier = self.build_verifier(
            WORKER_AUDIENCE
        )

        self.service = self.build_service()

    def build_verifier(self, audience):
        return IdentityVerifier(
            issuer=ISSUER,
            audience=audience,
            public_jwks=(self.public_jwk,),
        )

    def build_service(
        self,
        *,
        assignments=None,
        verifier=None,
    ):
        return WorkerScopeReviewService(
            identity_verifier=(
                self.verifier
                if verifier is None
                else verifier
            ),
            trusted_issuer=ISSUER,
            assignments=(
                (self.assignment,)
                if assignments is None
                else assignments
            ),
        )

    def sign(self, changes=None, *, key=None):
        now = int(time.time())

        claims = {
            "iss": ISSUER,
            "aud": WORKER_AUDIENCE,
            "sub": "worker-alpha",
            "iat": now - 30,
            "nbf": now - 30,
            "exp": now + 600,
        }

        if changes:
            claims.update(changes)

        return jwt.encode(
            claims,
            self.signing_key if key is None else key,
            algorithm="RS256",
            headers={
                "kid": "safeact-worker-review-test-key"
            },
        )

    def review(
        self,
        *,
        token=_UNSET,
        request=_UNSET,
        service=_UNSET,
    ):
        return (
            self.service if service is _UNSET else service
        ).inspect(
            token=self.sign() if token is _UNSET else token,
            request=self.request if request is _UNSET else request,
        )

    def test_exact_assignment_is_reviewable_but_never_dispatchable(self):
        reviewed = self.review()

        self.assertEqual(
            reviewed.worker_subject,
            "worker-alpha",
        )
        self.assertEqual(
            reviewed.operation_id,
            self.operation_id,
        )

        self.assertIs(
            reviewed.provider_dispatch_authorized,
            False,
        )
        self.assertIs(reviewed.retry_authorized, False)
        self.assertIs(
            reviewed.database_execution_authorized,
            False,
        )

    def test_unknown_subject_denied(self):
        with self.assertRaises(WorkerAccessDenied):
            self.review(
                token=self.sign({
                    "sub": "worker-unknown",
                })
            )

    def test_disabled_worker_denied(self):
        disabled = replace(
            self.assignment,
            enabled=False,
        )

        service = self.build_service(
            assignments=(disabled,),
        )

        with self.assertRaises(WorkerAccessDenied):
            self.review(service=service)

    def test_wrong_operation_denied(self):
        with self.assertRaises(WorkerAccessDenied):
            self.review(
                request=replace(
                    self.request,
                    operation_id=uuid4(),
                )
            )

    def test_wrong_tenant_denied(self):
        with self.assertRaises(WorkerAccessDenied):
            self.review(
                request=replace(
                    self.request,
                    tenant_id="tenant-other",
                )
            )

    def test_wrong_connector_denied(self):
        with self.assertRaises(WorkerAccessDenied):
            self.review(
                request=replace(
                    self.request,
                    connector_id="crm-other",
                )
            )

    def test_wrong_provider_account_denied(self):
        with self.assertRaises(WorkerAccessDenied):
            self.review(
                request=replace(
                    self.request,
                    provider_account_id="provider-other",
                )
            )

    def test_forged_signature_denied(self):
        with self.assertRaises(
            IdentityVerificationError
        ):
            self.review(
                token=self.sign(
                    key=self.attacker_key
                )
            )

    def test_admission_audience_denied(self):
        with self.assertRaises(
            IdentityVerificationError
        ):
            self.review(
                token=self.sign({
                    "aud": ADMISSION_AUDIENCE,
                })
            )

    def test_misconfigured_admission_verifier_still_denied(self):
        # Deliberately supply a verifier that accepts
        # admission-audience tokens. The separate worker
        # audience requirement must still prevent review.
        admission_verifier = self.build_verifier(
            ADMISSION_AUDIENCE
        )

        service = self.build_service(
            verifier=admission_verifier,
        )

        with self.assertRaises(WorkerAccessDenied):
            self.review(
                service=service,
                token=self.sign({
                    "aud": ADMISSION_AUDIENCE,
                }),
            )

    def test_wrong_issuer_denied(self):
        with self.assertRaises(
            IdentityVerificationError
        ):
            self.review(
                token=self.sign({
                    "iss": "https://untrusted.example",
                })
            )

    def test_expired_token_denied(self):
        now = int(time.time())

        with self.assertRaises(
            IdentityVerificationError
        ):
            self.review(
                token=self.sign({
                    "iat": now - 60,
                    "nbf": now - 60,
                    "exp": now - 10,
                })
            )

    def test_future_token_denied(self):
        now = int(time.time())

        with self.assertRaises(
            IdentityVerificationError
        ):
            self.review(
                token=self.sign({
                    "iat": now + 120,
                    "nbf": now + 120,
                    "exp": now + 600,
                })
            )

    def test_malformed_tokens_denied(self):
        for value in (None, 7, "", False):
            with self.subTest(value=repr(value)):
                with self.assertRaises(
                    WorkerAccessDenied
                ):
                    self.review(token=value)

        # Exercise the actual service without helper defaults.
        with self.assertRaises(WorkerAccessDenied):
            self.service.inspect(
                token=None,
                request=self.request,
            )

        with self.assertRaises(
            IdentityVerificationError
        ):
            self.review(token="not.a.valid.jwt")

    def test_conflicting_registry_assignments_rejected(self):
        with self.assertRaises(
            WorkerAuthorityConfigurationError
        ):
            self.build_service(
                assignments=(
                    self.assignment,
                    self.assignment,
                ),
            )

        with self.assertRaises(
            WorkerAuthorityConfigurationError
        ):
            self.build_service(
                assignments=[self.assignment],
            )

    def test_assignment_and_snapshot_cannot_be_replaced(self):
        with self.assertRaises(FrozenInstanceError):
            self.assignment.enabled = False

        with self.assertRaises(FrozenInstanceError):
            self.request.tenant_id = "tenant-other"

        # Constructing a separate disabled record does not
        # retroactively alter the trusted service snapshot.
        disabled = replace(
            self.assignment,
            enabled=False,
        )

        self.assertIs(disabled.enabled, False)

        reviewed = self.review()

        self.assertEqual(
            reviewed.operation_id,
            self.operation_id,
        )
        self.assertIs(
            reviewed.provider_dispatch_authorized,
            False,
        )

    def test_invalid_scope_and_untyped_request_rejected(self):
        with self.assertRaises(ValueError):
            WorkerScopeRequest(
                operation_id="not-a-uuid",
                tenant_id="tenant-alpha",
                connector_id="crm-alpha",
                provider_account_id="provider-alpha",
            )

        with self.assertRaises(ValueError):
            WorkerAssignment(
                subject="worker-alpha",
                operation_id=self.operation_id,
                tenant_id="tenant-alpha",
                connector_id="crm-alpha",
                provider_account_id="provider-alpha",
                enabled=1,
            )

        with self.assertRaises(WorkerAccessDenied):
            self.review(request=object())

        with self.assertRaises(WorkerAccessDenied):
            self.review(request=None)

        # Independently verify rejection by the actual service.
        with self.assertRaises(WorkerAccessDenied):
            self.service.inspect(
                token=self.sign(),
                request=None,
            )


if __name__ == "__main__":
    unittest.main()
