"""Pure authorization policy regression tests."""

import json
import unittest
from dataclasses import fields, replace

from core.admission_authorization import (
    AdmissionDenied,
    AdmissionGrant,
    InvalidAdmissionRequest,
    RequestedIntent,
    TrustedActorContext,
    authorize_intent,
)


class AdmissionAuthorizationTests(unittest.TestCase):

    def setUp(self):
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
                "priority": 1,
                "name": "café",
            },
        )

    def evaluate(
        self,
        *,
        context=None,
        grants=None,
        request=None,
    ):
        return authorize_intent(
            self.context if context is None else context,
            (self.grant,) if grants is None else grants,
            self.request if request is None else request,
        )

    def test_exact_scope_binds_trusted_tenant_and_actor(self):
        result = self.evaluate()

        self.assertEqual(
            result.tenant_id,
            self.context.tenant_id,
        )
        self.assertEqual(
            result.actor_id,
            self.context.actor_id,
        )
        self.assertEqual(
            json.loads(result.payload_canonical),
            self.request.payload,
        )

    def test_other_tenant_denied(self):
        grant = replace(
            self.grant,
            tenant_id="tenant-other",
        )

        with self.assertRaises(AdmissionDenied):
            self.evaluate(grants=(grant,))

    def test_other_actor_denied(self):
        grant = replace(
            self.grant,
            actor_id="actor-other",
        )

        with self.assertRaises(AdmissionDenied):
            self.evaluate(grants=(grant,))

    def test_other_action_denied(self):
        request = replace(
            self.request,
            action_type="delete_case",
        )

        with self.assertRaises(AdmissionDenied):
            self.evaluate(request=request)

    def test_other_connector_denied(self):
        request = replace(
            self.request,
            connector_id="other-connector",
        )

        with self.assertRaises(AdmissionDenied):
            self.evaluate(request=request)

    def test_other_provider_denied(self):
        request = replace(
            self.request,
            provider_account_id="other-account",
        )

        with self.assertRaises(AdmissionDenied):
            self.evaluate(request=request)

    def test_other_resource_denied(self):
        request = replace(
            self.request,
            resource_id="case-999",
        )

        with self.assertRaises(AdmissionDenied):
            self.evaluate(request=request)

    def test_other_contract_version_denied(self):
        request = replace(
            self.request,
            contract_version="v2",
        )

        with self.assertRaises(AdmissionDenied):
            self.evaluate(request=request)

    def test_no_grants_denied(self):
        with self.assertRaises(AdmissionDenied):
            self.evaluate(grants=())

    def test_untrusted_context_shape_denied(self):
        with self.assertRaises(AdmissionDenied):
            self.evaluate(context={
                "tenant_id": "tenant-alpha",
                "actor_id": "actor-alpha",
            })

    def test_untrusted_grant_collection_denied(self):
        with self.assertRaises(AdmissionDenied):
            self.evaluate(grants=[self.grant])

        with self.assertRaises(AdmissionDenied):
            self.evaluate(grants=(self.grant, object()))

    def test_wildcard_not_supported(self):
        grant = replace(
            self.grant,
            resource_id="*",
        )

        request = replace(
            self.request,
            resource_id="*",
        )

        with self.assertRaises(AdmissionDenied):
            self.evaluate(
                grants=(grant,),
                request=request,
            )

    def test_request_cannot_assert_tenant_or_actor(self):
        names = {
            item.name
            for item in fields(RequestedIntent)
        }

        self.assertNotIn("tenant_id", names)
        self.assertNotIn("actor_id", names)

        with self.assertRaises(TypeError):
            RequestedIntent(
                **{
                    "request_key": "key",
                    "action_type": "create_case",
                    "resource_id": "case-123",
                    "provider_account_id": "account-alpha",
                    "connector_id": "mock-crm",
                    "contract_version": "v1",
                    "payload": {},
                    "tenant_id": "attacker-tenant",
                }
            )

    def test_payload_is_canonical_snapshot(self):
        payload = {
            "nested": {
                "value": 1,
            }
        }

        request = replace(
            self.request,
            payload=payload,
        )

        authorized = self.evaluate(
            request=request
        )

        previous = authorized.payload_canonical

        payload["nested"]["value"] = 999

        self.assertEqual(
            authorized.payload_canonical,
            previous,
        )
        self.assertEqual(
            json.loads(authorized.payload_canonical),
            {"nested": {"value": 1}},
        )

    def test_bad_payload_fails_closed(self):
        for payload in (
            [],
            {"amount": 1.5},
            {"oversized": "x" * 65536},
        ):
            with self.subTest(kind=type(payload).__name__):
                request = replace(
                    self.request,
                    payload=payload,
                )

                with self.assertRaises(
                    InvalidAdmissionRequest
                ):
                    self.evaluate(request=request)

    def test_invalid_identity_or_request_strings_denied(self):
        context = replace(
            self.context,
            actor_id="",
        )

        with self.assertRaises(AdmissionDenied):
            self.evaluate(context=context)

        request = replace(
            self.request,
            action_type=" ",
        )

        with self.assertRaises(
            InvalidAdmissionRequest
        ):
            self.evaluate(request=request)

    def test_multiple_exact_scopes_allow_one_match(self):
        unrelated = replace(
            self.grant,
            resource_id="case-other",
        )

        result = self.evaluate(
            grants=(unrelated, self.grant)
        )

        self.assertEqual(
            result.resource_id,
            "case-123",
        )


if __name__ == "__main__":
    unittest.main()
