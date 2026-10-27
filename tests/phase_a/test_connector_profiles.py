"""Connector profile validation and row round trip. No database."""

import unittest

from dataclasses import replace
from datetime import timedelta

from core.connector_profiles import (
    MOCK_PROFILES,
    PROFILE_COLUMNS,
    SideEffectClass,
    crm_profile,
    email_profile,
    payments_profile,
    profile_from_row,
)
from core.contracts import LookupSemantics


class ConnectorProfileTests(unittest.TestCase):

    def test_mock_profiles_round_trip_through_rows(self):
        for profile in MOCK_PROFILES:
            with self.subTest(profile=(profile.connector_id, profile.version)):
                row = profile.row()
                self.assertEqual(tuple(row), PROFILE_COLUMNS)
                self.assertEqual(
                    profile_from_row(tuple(row[c] for c in PROFILE_COLUMNS)),
                    profile,
                )

    def test_mock_profile_identities_are_unique(self):
        keys = [(p.connector_id, p.version) for p in MOCK_PROFILES]
        self.assertEqual(len(keys), len(set(keys)))

    def test_safety_margin_must_cover_request_deadline(self):
        with self.assertRaisesRegex(ValueError, "request deadline"):
            payments_profile(
                safety_margin=timedelta(seconds=29),
                request_deadline=timedelta(seconds=30),
            )
        # Equal is enough: the request is sent before the cutoff.
        payments_profile(
            safety_margin=timedelta(seconds=30),
            request_deadline=timedelta(seconds=30),
        )

    def test_profiles_without_native_key_need_no_margin(self):
        self.assertIsNone(crm_profile().contract.native_idempotency)
        self.assertIsNone(email_profile().contract.native_idempotency)

    def test_email_profile_escalates_at_once_and_never_retries(self):
        email = email_profile()
        self.assertEqual(email.investigation_window, timedelta(0))
        self.assertEqual(email.max_attempts, 1)
        # Evidence-triggered reopen only; there is no lookup.
        self.assertEqual(email.max_reopens, 1)
        self.assertIs(
            email.contract.lookup_semantics, LookupSemantics.UNAVAILABLE
        )
        self.assertIs(
            email.side_effect_class, SideEffectClass.IRREVERSIBLE_MESSAGE
        )

    def test_only_payments_declares_a_compensation(self):
        self.assertEqual(payments_profile().compensation_action, "refund_payment")
        self.assertIsNone(crm_profile().compensation_action)
        self.assertIsNone(email_profile().compensation_action)

    def test_invalid_values_are_rejected(self):
        base = payments_profile()
        for changes in (
            {"request_deadline": timedelta(0)},
            {"request_deadline": timedelta(seconds=1.5)},
            {"investigation_window": timedelta(seconds=-1)},
            {"post_escalation_check_interval": timedelta(0)},
            {"max_attempts": 0},
            {"max_reopens": -1},
            {"max_attempts": True},
            {"compensation_action": " "},
            {"contract_reference": ""},
            {"side_effect_class": "reversible_payment"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    replace(base, **changes)


if __name__ == "__main__":
    unittest.main()
