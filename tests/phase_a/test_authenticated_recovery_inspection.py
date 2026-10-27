"""Authenticated recovery inspection integration-style regressions.

Real boundaries exercised:
- RS256 identity verification
- Authoritative principal-to-actor resolution
- Exact-scope admission authorization
- Immutable authorized submission construction
- Negative-only recovery inspection

Only the low-level database observation call is mocked.

No PostgreSQL connection, real provider effect, or dispatch
authorization is used in these tests.

AuthorityRegistry is an immutable local authority snapshot.
Disabling an actor in these tests creates a new registry;
this does not demonstrate live revocation propagation.
"""

import json
import time
import unittest

from dataclasses import replace
from unittest.mock import patch, sentinel
from uuid import UUID

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

from storage.recovery_inspection import (
    RecoveryInspectionVerdict,
    inspect_recovery_admission,
)

from storage.submission import (
    IntentObservation,
    IntentObservationStatus,
    IntentSubmission,
    SubmissionConflict,
    SubmissionStateUncertain,
)


ISSUER = "https://id.safeact.example"
AUDIENCE = "safeact-admission"

OPERATION_ID = UUID(
    "00000000-0000-4000-8000-000000000022"
)

OBSERVATION_TARGET = (
    "storage.authorized_submission.observe_intent"
)


class PoisonEngine:
    """Any unmocked database access fails this test."""

    def begin(self):
        raise AssertionError(
            "Unexpected database transaction"
        )

    def connect(self):
        raise AssertionError(
            "Unexpected database connection"
        )


class AuthenticatedRecoveryInspectionTests(unittest.TestCase):

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
            kid="recovery-test-key",
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
            connector_id="mock-crm",
            provider_account_id="account-alpha",
            action_type="create_case",
            resource_id="case-123",
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
        selected = (
            self.record if record is None else record
        )

        registry = AuthorityRegistry(
            audience=AUDIENCE,
            records=(selected,),
        )

        return AuthenticatedAdmissionService(
            engine=self.engine,
            identity_verifier=self.verifier,
            authority_registry=registry,
        )

    def sign(self, changes=None, *, key=None):
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

        return jwt.encode(
            claims,
            self.signing_key if key is None else key,
            algorithm="RS256",
            headers={"kid": "recovery-test-key"},
        )

    def invoke(
        self,
        *,
        observed,
        token=None,
        request=None,
        service=None,
    ):
        selected_token = (
            self.sign() if token is None else token
        )

        selected_request = (
            self.request if request is None else request
        )

        selected_service = (
            self.service if service is None else service
        )

        with patch(
            OBSERVATION_TARGET,
            return_value=observed,
        ) as database_observer:
            result = inspect_recovery_admission(
                admission_service=selected_service,
                token=selected_token,
                request=selected_request,
            )

        database_observer.assert_called_once()

        return result, database_observer

    def assert_denied_before_database(
        self,
        error_type,
        *,
        token=None,
        request=None,
        service=None,
    ):
        selected_token = (
            self.sign() if token is None else token
        )

        selected_request = (
            self.request if request is None else request
        )

        selected_service = (
            self.service if service is None else service
        )

        with patch(
            OBSERVATION_TARGET,
            return_value=sentinel.unreachable,
        ) as database_observer:
            with self.assertRaises(error_type):
                inspect_recovery_admission(
                    admission_service=selected_service,
                    token=selected_token,
                    request=selected_request,
                )

            database_observer.assert_not_called()

    def test_valid_token_unknown_admission_is_held(self):
        result, observer = self.invoke(
            observed=IntentObservation(
                status=IntentObservationStatus.UNKNOWN,
            )
        )

        self.assertEqual(
            result.verdict,
            RecoveryInspectionVerdict.HOLD_ADMISSION_UNKNOWN,
        )

        self.assertIsNone(result.operation_id)
        self.assertIs(result.retry_allowed, False)
        self.assertIs(
            result.provider_dispatch_authorized,
            False,
        )

        self.assertEqual(observer.call_count, 1)

    def test_valid_token_recorded_admission_is_held(self):
        result, _ = self.invoke(
            observed=IntentObservation(
                status=(
                    IntentObservationStatus.CONFIRMED_RECORDED
                ),
                operation_id=OPERATION_ID,
            )
        )

        self.assertEqual(
            result.verdict,
            RecoveryInspectionVerdict.HOLD_PROVIDER_UNVERIFIED,
        )

        self.assertEqual(
            result.operation_id,
            OPERATION_ID,
        )

        self.assertIs(result.retry_allowed, False)
        self.assertIs(
            result.provider_dispatch_authorized,
            False,
        )

    def test_database_receives_authorized_identity_and_payload(self):
        _, observer = self.invoke(
            observed=IntentObservation(
                status=IntentObservationStatus.UNKNOWN,
            )
        )

        engine, submission = observer.call_args.args

        self.assertIs(engine, self.engine)
        self.assertIsInstance(
            submission, IntentSubmission
        )

        self.assertEqual(
            submission.tenant_id,
            "tenant-alpha",
        )

        self.assertEqual(
            submission.actor_id,
            "actor-alpha",
        )

        self.assertEqual(
            submission.request_key,
            "request-123",
        )

        self.assertEqual(
            submission.resource_id,
            "case-123",
        )

        self.assertEqual(
            submission.provider_account_id,
            "account-alpha",
        )

        self.assertEqual(
            submission.payload,
            {"case_id": "case-123"},
        )

    def test_signed_roles_and_identity_claims_do_not_override_registry(self):
        token = self.sign({
            "tenant_id": "attacker-tenant",
            "actor_id": "attacker-actor",
            "roles": ["superadmin"],
        })

        result, observer = self.invoke(
            token=token,
            observed=IntentObservation(
                status=IntentObservationStatus.UNKNOWN,
            ),
        )

        _, submission = observer.call_args.args

        self.assertEqual(
            submission.tenant_id,
            "tenant-alpha",
        )

        self.assertEqual(
            submission.actor_id,
            "actor-alpha",
        )

        self.assertIs(result.retry_allowed, False)

    def test_malformed_token_stops_before_database(self):
        self.assert_denied_before_database(
            IdentityVerificationError,
            token="not.a.valid.token",
        )

    def test_attacker_signature_stops_before_database(self):
        self.assert_denied_before_database(
            IdentityVerificationError,
            token=self.sign(key=self.attacker_key),
        )

    def test_expired_token_stops_before_database(self):
        now = int(time.time())

        self.assert_denied_before_database(
            IdentityVerificationError,
            token=self.sign({
                "iat": now - 120,
                "nbf": now - 120,
                "exp": now - 1,
            }),
        )

    def test_wrong_audience_stops_before_database(self):
        self.assert_denied_before_database(
            IdentityVerificationError,
            token=self.sign({
                "aud": "another-service",
            }),
        )

    def test_unknown_subject_stops_before_database(self):
        self.assert_denied_before_database(
            AuthorityResolutionDenied,
            token=self.sign({
                "sub": "unregistered-subject",
            }),
        )

    def test_disabled_actor_snapshot_stops_before_database(self):
        disabled = replace(
            self.record,
            enabled=False,
        )

        self.assert_denied_before_database(
            AuthorityResolutionDenied,
            service=self.make_service(disabled),
        )

    def test_missing_grants_stops_before_database(self):
        no_grants = replace(
            self.record,
            grants=(),
        )

        self.assert_denied_before_database(
            AuthorityResolutionDenied,
            service=self.make_service(no_grants),
        )

    def test_other_resource_stops_before_database(self):
        different_request = replace(
            self.request,
            resource_id="another-case",
        )

        self.assert_denied_before_database(
            AdmissionDenied,
            request=different_request,
        )

    def test_other_provider_stops_before_database(self):
        different_request = replace(
            self.request,
            provider_account_id="another-provider",
        )

        self.assert_denied_before_database(
            AdmissionDenied,
            request=different_request,
        )

    def test_caller_cannot_override_retry_authorization(self):
        with patch(
            OBSERVATION_TARGET,
        ) as database_observer:
            with self.assertRaises(TypeError):
                inspect_recovery_admission(
                    admission_service=self.service,
                    token=self.sign(),
                    request=self.request,
                    retry_authorized=True,
                )

            database_observer.assert_not_called()

    def test_database_read_failure_propagates(self):
        with patch(
            OBSERVATION_TARGET,
            side_effect=SubmissionStateUncertain(
                "synthetic database read failure"
            ),
        ) as database_observer:
            with self.assertRaises(
                SubmissionStateUncertain
            ):
                inspect_recovery_admission(
                    admission_service=self.service,
                    token=self.sign(),
                    request=self.request,
                )

            database_observer.assert_called_once()

    def test_actor_ownership_conflict_propagates(self):
        with patch(
            OBSERVATION_TARGET,
            side_effect=SubmissionConflict(
                "synthetic original actor mismatch"
            ),
        ) as database_observer:
            with self.assertRaises(SubmissionConflict):
                inspect_recovery_admission(
                    admission_service=self.service,
                    token=self.sign(),
                    request=self.request,
                )

            database_observer.assert_called_once()

    def test_malformed_database_observation_fails_closed(self):
        with patch(
            OBSERVATION_TARGET,
            return_value={
                "status": "CONFIRMED_RECORDED",
                "retry_allowed": True,
            },
        ) as database_observer:
            with self.assertRaises(
                SubmissionStateUncertain
            ):
                inspect_recovery_admission(
                    admission_service=self.service,
                    token=self.sign(),
                    request=self.request,
                )

            database_observer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
