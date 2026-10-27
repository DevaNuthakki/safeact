"""Actor-scoped admission replay behavior on disposable PostgreSQL."""

import unittest
from dataclasses import replace

from core.admission_authorization import (
    AdmissionDenied,
    AdmissionGrant,
    RequestedIntent,
    TrustedActorContext,
)
from storage.authorized_submission import (
    observe_scoped_intent,
    submit_scoped_intent,
)
from storage.submission import (
    IntentObservationStatus,
    SubmissionConflict,
)
from tests.integration import test_submission_commits as fixtures


class ActorKeyOwnershipTests(unittest.TestCase):

    def setUp(self):
        # Reuse the established schema-isolated committed
        # submission fixture without importing its TestCase
        # into this module's unittest discovery namespace.
        probe = fixtures.CommittedSubmissionTests(
            methodName=(
                "test_engine_submission_commits_complete_bundle"
            )
        )

        self.addCleanup(probe.doCleanups)
        probe.setUp()

        self.probe = probe
        base = probe.request()

        self.request = RequestedIntent(
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

        self.actor_a = TrustedActorContext(
            tenant_id=base.tenant_id,
            actor_id=base.actor_id,
        )

        self.grant_a = AdmissionGrant(
            tenant_id=self.actor_a.tenant_id,
            actor_id=self.actor_a.actor_id,
            action_type=base.action_type,
            resource_id=base.resource_id,
            provider_account_id=base.provider_account_id,
            connector_id=base.connector_id,
            contract_version=base.contract_version,
        )

        self.actor_b = replace(
            self.actor_a,
            actor_id="synthetic-other-actor",
        )

        self.grant_b = replace(
            self.grant_a,
            actor_id=self.actor_b.actor_id,
        )

    def submit(self, actor, grant):
        return submit_scoped_intent(
            self.probe.runtime_engine,
            actor,
            (grant,),
            self.request,
        )

    def observe(self, actor, grant):
        return observe_scoped_intent(
            self.probe.runtime_engine,
            actor,
            (grant,),
            self.request,
        )

    def test_other_granted_actor_cannot_claim_existing_key(self):
        first = self.submit(
            self.actor_a, self.grant_a
        )

        self.assertTrue(first.created)

        # Actor B is authorized for the action itself,
        # but does not own Actor A's request key.
        with self.assertRaises(SubmissionConflict):
            self.observe(
                self.actor_b, self.grant_b
            )

        with self.assertRaises(SubmissionConflict):
            self.submit(
                self.actor_b, self.grant_b
            )

        self.assertEqual(
            self.probe.counts(),
            (1, 1, 1, 0),
        )

        original_observation = self.observe(
            self.actor_a, self.grant_a
        )

        self.assertEqual(
            original_observation.status,
            IntentObservationStatus.CONFIRMED_RECORDED,
        )

        self.assertEqual(
            original_observation.operation_id,
            first.operation_id,
        )

        replay = self.submit(
            self.actor_a, self.grant_a
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

    def test_ungranted_actor_still_denied_without_writes(self):
        with self.assertRaises(AdmissionDenied):
            self.submit(
                self.actor_b, self.grant_a
            )

        with self.assertRaises(AdmissionDenied):
            self.observe(
                self.actor_b, self.grant_a
            )

        self.assertEqual(
            self.probe.counts(),
            (0, 0, 0, 0),
        )

    def test_other_tenant_keeps_independent_key_namespace(self):
        first = self.submit(
            self.actor_a, self.grant_a
        )

        other_tenant = "synthetic-other-tenant"

        self.assertNotEqual(
            other_tenant,
            self.actor_a.tenant_id,
        )

        actor_other_tenant = TrustedActorContext(
            tenant_id=other_tenant,
            actor_id="synthetic-other-actor",
        )

        grant_other_tenant = replace(
            self.grant_a,
            tenant_id=other_tenant,
            actor_id=actor_other_tenant.actor_id,
        )

        second = self.submit(
            actor_other_tenant,
            grant_other_tenant,
        )

        self.assertTrue(first.created)
        self.assertTrue(second.created)
        self.assertNotEqual(
            first.operation_id,
            second.operation_id,
        )

        self.assertEqual(
            self.probe.counts(),
            (2, 2, 2, 0),
        )


if __name__ == "__main__":
    unittest.main()
