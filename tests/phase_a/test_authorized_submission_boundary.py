"""Test authorization occurs before storage is invoked."""

import unittest
from dataclasses import replace
from unittest.mock import patch

from core.admission_authorization import (
    AdmissionDenied,
    AdmissionGrant,
    InvalidAdmissionRequest,
    RequestedIntent,
    TrustedActorContext,
    authorize_intent,
)
from storage.authorized_submission import (
    observe_scoped_intent,
    submit_scoped_intent,
)
from core.provider_keys import derive_provider_key
from storage.submission import (
    IntentSubmission,
    SubmissionValidationError,
)


class AuthorizedSubmissionBoundaryTests(unittest.TestCase):

    def setUp(self):
        self.engine = object()

        self.context = TrustedActorContext(
            tenant_id="tenant-alpha",
            actor_id="actor-alpha",
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

        self.request = RequestedIntent(
            request_key="request-001",
            action_type="create_case",
            resource_id="case-123",
            provider_account_id="account-alpha",
            connector_id="mock-crm",
            contract_version="v1",
            payload={
                "name": "café",
                "nested": {"priority": 1},
            },
        )

    def submit(
        self, *, context=None, grants=None, request=None
    ):
        return submit_scoped_intent(
            self.engine,
            self.context if context is None else context,
            (self.grant,) if grants is None else grants,
            self.request if request is None else request,
        )

    def observe(
        self, *, context=None, grants=None, request=None
    ):
        return observe_scoped_intent(
            self.engine,
            self.context if context is None else context,
            (self.grant,) if grants is None else grants,
            self.request if request is None else request,
        )

    def assert_submit_denied(self, **kwargs):
        with patch(
            "storage.authorized_submission.submit_intent"
        ) as backend:
            with self.assertRaises(AdmissionDenied):
                self.submit(**kwargs)

            backend.assert_not_called()

    def test_submit_binds_trusted_identity(self):
        receipt = object()

        with patch(
            "storage.authorized_submission.submit_intent",
            return_value=receipt,
        ) as backend:
            result = self.submit()

        self.assertIs(result, receipt)
        backend.assert_called_once()

        engine, submission = backend.call_args.args

        self.assertIs(engine, self.engine)
        self.assertIsInstance(
            submission, IntentSubmission
        )
        self.assertEqual(
            submission.tenant_id, "tenant-alpha"
        )
        self.assertEqual(
            submission.actor_id, "actor-alpha"
        )

    def test_submit_preserves_payload_and_derives_provider_key(self):
        with patch(
            "storage.authorized_submission.submit_intent"
        ) as backend:
            self.submit(request=self.request)

        submission = backend.call_args.args[1]

        self.assertEqual(
            submission.payload, self.request.payload
        )
        # The key comes from operation identity, never the caller.
        self.assertEqual(
            submission.provider_idempotency_key,
            derive_provider_key(
                submission.tenant_id, submission.request_key,
                submission.connector_id,
                submission.provider_account_id,
            ),
        )

    def test_caller_chosen_provider_key_is_refused(self):
        request = replace(
            self.request,
            provider_idempotency_key="provider-key-001",
        )

        with patch(
            "storage.authorized_submission.submit_intent"
        ) as backend:
            with self.assertRaises(SubmissionValidationError):
                self.submit(request=request)

        backend.assert_not_called()

    def test_payload_mutation_after_policy_cannot_change_write(self):
        payload = {"nested": {"priority": 1}}
        request = replace(
            self.request,
            payload=payload,
        )

        authorized = authorize_intent(
            self.context, (self.grant,), request
        )

        def mutate_after_authorization(*args):
            payload["nested"]["priority"] = 999
            return authorized

        with patch(
            "storage.authorized_submission.authorize_intent",
            side_effect=mutate_after_authorization,
        ):
            with patch(
                "storage.authorized_submission.submit_intent"
            ) as backend:
                self.submit(request=request)

        submitted = backend.call_args.args[1]

        self.assertEqual(
            submitted.payload,
            {"nested": {"priority": 1}},
        )
        self.assertEqual(
            payload["nested"]["priority"], 999
        )

    def test_other_tenant_denied_before_storage(self):
        self.assert_submit_denied(
            context=replace(
                self.context, tenant_id="other-tenant"
            )
        )

    def test_other_actor_denied_before_storage(self):
        self.assert_submit_denied(
            context=replace(
                self.context, actor_id="other-actor"
            )
        )

    def test_other_action_or_provider_denied_before_storage(self):
        for field, value in (
            ("action_type", "delete_case"),
            ("provider_account_id", "other-account"),
        ):
            with self.subTest(field=field):
                self.assert_submit_denied(
                    request=replace(
                        self.request, **{field: value}
                    )
                )

    def test_missing_grant_denied_before_storage(self):
        self.assert_submit_denied(grants=())

    def test_invalid_context_denied_before_storage(self):
        self.assert_submit_denied(
            context={
                "tenant_id": "tenant-alpha",
                "actor_id": "actor-alpha",
            }
        )

    def test_wildcard_grant_denied_before_storage(self):
        self.assert_submit_denied(
            grants=(
                replace(
                    self.grant, resource_id="*"
                ),
            )
        )

    def test_malformed_payload_never_reaches_storage(self):
        request = replace(
            self.request, payload={"amount": 1.5}
        )

        with patch(
            "storage.authorized_submission.submit_intent"
        ) as backend:
            with self.assertRaises(
                InvalidAdmissionRequest
            ):
                self.submit(request=request)

            backend.assert_not_called()

    def test_denied_observation_never_reaches_database(self):
        request = replace(
            self.request, resource_id="other-resource"
        )

        with patch(
            "storage.authorized_submission.observe_intent"
        ) as backend:
            with self.assertRaises(AdmissionDenied):
                self.observe(request=request)

            backend.assert_not_called()

    def test_authorized_observation_binds_identity(self):
        observed = object()

        with patch(
            "storage.authorized_submission.observe_intent",
            return_value=observed,
        ) as backend:
            result = self.observe()

        self.assertIs(result, observed)
        backend.assert_called_once()

        engine, submission = backend.call_args.args

        self.assertIs(engine, self.engine)
        self.assertEqual(
            submission.tenant_id, "tenant-alpha"
        )
        self.assertEqual(
            submission.actor_id, "actor-alpha"
        )
        self.assertEqual(
            submission.payload, self.request.payload
        )

    def test_commit_error_is_not_auto_retried(self):
        with patch(
            "storage.authorized_submission.submit_intent",
            side_effect=ConnectionError(
                "synthetic ambiguous commit"
            ),
        ) as backend:
            with self.assertRaises(ConnectionError):
                self.submit()

            backend.assert_called_once()

    def test_observation_error_is_not_auto_retried(self):
        with patch(
            "storage.authorized_submission.observe_intent",
            side_effect=ConnectionError(
                "synthetic observation failure"
            ),
        ) as backend:
            with self.assertRaises(ConnectionError):
                self.observe()

            backend.assert_called_once()


if __name__ == "__main__":
    unittest.main()
