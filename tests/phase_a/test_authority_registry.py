"""Tests for trusted actor-to-scope registry behavior."""

import unittest
from dataclasses import replace

from core.admission_authorization import (
    AdmissionGrant,
    TrustedActorContext,
)
from core.authority_registry import (
    ActorAuthorityRecord,
    AuthorityConfigurationError,
    AuthorityRegistry,
    AuthorityResolutionDenied,
)
from core.verified_identity import VerifiedPrincipal


ISSUER = "https://id.safeact.example"
AUDIENCE = "safeact-admission"


class AuthorityRegistryTests(unittest.TestCase):

    def setUp(self):
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
            subject="provider-subject-alpha",
            tenant_id="tenant-alpha",
            actor_id="actor-alpha",
            grants=(self.grant,),
        )

        self.principal = VerifiedPrincipal(
            issuer=ISSUER,
            subject="provider-subject-alpha",
            audience=AUDIENCE,
            issued_at=100,
            expires_at=200,
        )

    def registry(self, records=None):
        return AuthorityRegistry(
            audience=AUDIENCE,
            records=(
                (self.record,)
                if records is None else records
            ),
        )

    def test_resolves_exact_actor_and_scope(self):
        context, grants = self.registry().resolve_verified(
            self.principal
        )
        self.assertIsInstance(context, TrustedActorContext)
        self.assertEqual(context.tenant_id, "tenant-alpha")
        self.assertEqual(context.actor_id, "actor-alpha")
        self.assertEqual(grants, (self.grant,))

    def test_unknown_subject_denied(self):
        principal = replace(
            self.principal, subject="unknown-subject"
        )
        with self.assertRaises(AuthorityResolutionDenied):
            self.registry().resolve_verified(principal)

    def test_different_issuer_denied(self):
        principal = replace(
            self.principal, issuer="https://other.example"
        )
        with self.assertRaises(AuthorityResolutionDenied):
            self.registry().resolve_verified(principal)

    def test_wrong_audience_denied(self):
        principal = replace(
            self.principal, audience="other-service"
        )
        with self.assertRaises(AuthorityResolutionDenied):
            self.registry().resolve_verified(principal)

    def test_disabled_actor_denied(self):
        record = replace(self.record, enabled=False)
        with self.assertRaises(AuthorityResolutionDenied):
            self.registry((record,)).resolve_verified(
                self.principal
            )

    def test_no_grants_denied(self):
        record = replace(self.record, grants=())
        with self.assertRaises(AuthorityResolutionDenied):
            self.registry((record,)).resolve_verified(
                self.principal
            )

    def test_nonprincipal_denied(self):
        with self.assertRaises(AuthorityResolutionDenied):
            self.registry().resolve_verified({
                "issuer": ISSUER,
                "subject": "provider-subject-alpha",
            })

    def test_duplicate_provider_identity_rejected(self):
        other = replace(
            self.record,
            actor_id="actor-other",
            grants=(
                replace(self.grant, actor_id="actor-other"),
            ),
        )
        with self.assertRaises(AuthorityConfigurationError):
            self.registry((self.record, other))

    def test_duplicate_internal_actor_rejected(self):
        other = replace(
            self.record, subject="another-provider-subject"
        )
        with self.assertRaises(AuthorityConfigurationError):
            self.registry((self.record, other))

    def test_grant_tenant_mismatch_rejected(self):
        record = replace(
            self.record,
            grants=(
                replace(
                    self.grant, tenant_id="another-tenant"
                ),
            ),
        )
        with self.assertRaises(AuthorityConfigurationError):
            self.registry((record,))

    def test_grant_actor_mismatch_rejected(self):
        record = replace(
            self.record,
            grants=(
                replace(
                    self.grant, actor_id="another-actor"
                ),
            ),
        )
        with self.assertRaises(AuthorityConfigurationError):
            self.registry((record,))

    def test_wildcard_grant_rejected(self):
        record = replace(
            self.record,
            grants=(
                replace(self.grant, resource_id="*"),
            ),
        )
        with self.assertRaises(AuthorityConfigurationError):
            self.registry((record,))

    def test_invalid_binding_identifiers_rejected(self):
        changes = (
            {"issuer": ""},
            {"subject": " "},
            {"tenant_id": " tenant-alpha "},
            {"actor_id": "a\nb"},
        )

        for change in changes:
            with self.subTest(change=change):
                with self.assertRaises(
                    AuthorityConfigurationError
                ):
                    self.registry((
                        replace(self.record, **change),
                    ))

    def test_invalid_enabled_flag_rejected(self):
        with self.assertRaises(AuthorityConfigurationError):
            self.registry((
                replace(self.record, enabled=1),
            ))

    def test_invalid_records_container_rejected(self):
        for records in (
            [self.record],
            "incorrect",
            3,
        ):
            with self.subTest(value=str(type(records))):
                with self.assertRaises(
                    AuthorityConfigurationError
                ):
                    self.registry(records)

    def test_invalid_grant_container_rejected(self):
        with self.assertRaises(AuthorityConfigurationError):
            self.registry((
                replace(self.record, grants=[self.grant]),
            ))

    def test_invalid_grant_object_rejected(self):
        with self.assertRaises(AuthorityConfigurationError):
            self.registry((
                replace(self.record, grants=(object(),)),
            ))

    def test_duplicate_grant_rejected(self):
        with self.assertRaises(AuthorityConfigurationError):
            self.registry((
                replace(
                    self.record,
                    grants=(self.grant, self.grant),
                ),
            ))

    def test_two_actors_remain_separate(self):
        second_grant = replace(
            self.grant,
            tenant_id="tenant-beta",
            actor_id="actor-beta",
        )
        second = ActorAuthorityRecord(
            issuer=ISSUER,
            subject="provider-subject-beta",
            tenant_id="tenant-beta",
            actor_id="actor-beta",
            grants=(second_grant,),
        )
        registry = self.registry((self.record, second))

        first_context, first_grants = registry.resolve_verified(
            self.principal
        )
        second_context, second_grants = registry.resolve_verified(
            replace(
                self.principal,
                subject="provider-subject-beta",
            )
        )

        self.assertNotEqual(first_context, second_context)
        self.assertEqual(first_grants, (self.grant,))
        self.assertEqual(second_grants, (second_grant,))

    def test_empty_registry_denies(self):
        registry = self.registry(())
        with self.assertRaises(AuthorityResolutionDenied):
            registry.resolve_verified(self.principal)


if __name__ == "__main__":
    unittest.main()
