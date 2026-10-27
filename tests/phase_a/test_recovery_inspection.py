"""Read-only recovery inspection unit and fault-boundary tests.

The authenticated service method is mocked here to isolate
inspection semantics. Existing authenticated-boundary tests
separately verify token and authority resolution.

These are not live provider-dispatch tests.
"""

import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch, sentinel
from uuid import UUID

from core.admission_authorization import AdmissionDenied
from storage.authenticated_submission import (
    AuthenticatedAdmissionService,
)
from storage.recovery_inspection import (
    RecoveryInspection,
    RecoveryInspectionVerdict,
    inspect_recovery_admission,
)
from storage.submission import (
    IntentObservation,
    IntentObservationStatus,
    SubmissionConflict,
    SubmissionStateUncertain,
)


OPERATION_ID = UUID(
    "00000000-0000-4000-8000-000000000011"
)


class RecoveryInspectionTests(unittest.TestCase):

    def setUp(self):
        # Deliberately uninitialized instance for mocked unit
        # testing only. Never used as an authentication fixture.
        self.service = object.__new__(
            AuthenticatedAdmissionService
        )

    def inspect(self, observed):
        with patch.object(
            AuthenticatedAdmissionService,
            "observe",
            return_value=observed,
        ) as backend:
            result = inspect_recovery_admission(
                admission_service=self.service,
                token="synthetic-test-token",
                request=sentinel.request,
            )

        backend.assert_called_once_with(
            token="synthetic-test-token",
            request=sentinel.request,
        )

        return result

    def test_unknown_admission_is_held(self):
        result = self.inspect(
            IntentObservation(
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

    def test_recorded_admission_does_not_prove_provider_effect(self):
        result = self.inspect(
            IntentObservation(
                status=IntentObservationStatus.CONFIRMED_RECORDED,
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

    def test_missing_observation_is_rejected(self):
        with self.assertRaises(SubmissionStateUncertain):
            self.inspect(None)

    def test_unknown_with_operation_identity_is_rejected(self):
        with self.assertRaises(SubmissionStateUncertain):
            self.inspect(
                IntentObservation(
                    status=IntentObservationStatus.UNKNOWN,
                    operation_id=OPERATION_ID,
                )
            )

    def test_recorded_without_operation_identity_is_rejected(self):
        with self.assertRaises(SubmissionStateUncertain):
            self.inspect(
                IntentObservation(
                    status=(
                        IntentObservationStatus.CONFIRMED_RECORDED
                    ),
                )
            )

    def test_recorded_with_untyped_identity_is_rejected(self):
        with self.assertRaises(SubmissionStateUncertain):
            self.inspect(
                IntentObservation(
                    status=(
                        IntentObservationStatus.CONFIRMED_RECORDED
                    ),
                    operation_id=str(OPERATION_ID),
                )
            )

    def test_string_status_cannot_impersonate_enum(self):
        with self.assertRaises(SubmissionStateUncertain):
            self.inspect(
                IntentObservation(
                    status="UNKNOWN",
                )
            )

    def test_unsupported_observation_status_is_rejected(self):
        with self.assertRaises(SubmissionStateUncertain):
            self.inspect(
                IntentObservation(
                    status="PROVIDER_APPLIED",
                )
            )

    def test_revoked_authority_denial_propagates(self):
        with patch.object(
            AuthenticatedAdmissionService,
            "observe",
            side_effect=AdmissionDenied(
                "synthetic revoked authority"
            ),
        ) as backend:
            with self.assertRaises(AdmissionDenied):
                inspect_recovery_admission(
                    admission_service=self.service,
                    token="synthetic-test-token",
                    request=sentinel.request,
                )

        backend.assert_called_once()

    def test_actor_identity_conflict_propagates(self):
        with patch.object(
            AuthenticatedAdmissionService,
            "observe",
            side_effect=SubmissionConflict(
                "synthetic actor mismatch"
            ),
        ):
            with self.assertRaises(SubmissionConflict):
                inspect_recovery_admission(
                    admission_service=self.service,
                    token="synthetic-test-token",
                    request=sentinel.request,
                )

    def test_database_observation_failure_propagates(self):
        with patch.object(
            AuthenticatedAdmissionService,
            "observe",
            side_effect=SubmissionStateUncertain(
                "synthetic observation failure"
            ),
        ):
            with self.assertRaises(SubmissionStateUncertain):
                inspect_recovery_admission(
                    admission_service=self.service,
                    token="synthetic-test-token",
                    request=sentinel.request,
                )

    def test_caller_cannot_supply_retry_permission(self):
        with patch.object(
            AuthenticatedAdmissionService,
            "observe",
        ) as backend:
            with self.assertRaises(TypeError):
                inspect_recovery_admission(
                    admission_service=self.service,
                    token="synthetic-test-token",
                    request=sentinel.request,
                    retry_authorized=True,
                )

            backend.assert_not_called()

    def test_untrusted_service_object_is_rejected(self):
        with self.assertRaises(TypeError):
            inspect_recovery_admission(
                admission_service=object(),
                token="synthetic-test-token",
                request=sentinel.request,
            )

    def test_inspection_cannot_be_mutated_into_allow(self):
        result = RecoveryInspection(
            verdict=RecoveryInspectionVerdict.HOLD_ADMISSION_UNKNOWN,
            operation_id=None,
        )

        with self.assertRaises(FrozenInstanceError):
            result.verdict = (
                RecoveryInspectionVerdict.HOLD_PROVIDER_UNVERIFIED
            )

        with self.assertRaises(
            (AttributeError, FrozenInstanceError, TypeError)
        ):
            result.retry_allowed = True

        self.assertFalse(result.retry_allowed)
        self.assertFalse(result.provider_dispatch_authorized)

    def test_inconsistent_inspection_construction_rejected(self):
        with self.assertRaises(ValueError):
            RecoveryInspection(
                verdict=RecoveryInspectionVerdict.HOLD_ADMISSION_UNKNOWN,
                operation_id=OPERATION_ID,
            )

        with self.assertRaises(ValueError):
            RecoveryInspection(
                verdict=RecoveryInspectionVerdict.HOLD_PROVIDER_UNVERIFIED,
                operation_id=None,
            )


if __name__ == "__main__":
    unittest.main()
