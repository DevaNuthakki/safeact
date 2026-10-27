"""Preparation reruns must preserve undecided cases and captured evidence."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import prepare_walkthrough as prep


class PreparationRerunTests(unittest.TestCase):
    def test_open_email_proposal_is_reused_even_close_to_expiry(self):
        proposal = {"operation_id": "email", "proposal_id": "email-proposal",
                    "action": "ATTEST_APPLIED", "attested_reference": prep.ATTESTED_REFERENCE,
                    "status": "open", "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()}
        reviewer = Mock()
        reviewer.get.return_value = {"items": [proposal]}
        with patch.object(prep, "propose") as propose:
            self.assertEqual(prep.choose_email_proposal(Mock(), reviewer,
                             {"operation_id": "email", "state": "ESCALATED_UNRESOLVED"}), proposal)
            propose.assert_not_called()

    def test_expired_email_gets_a_new_proposal_without_a_new_email(self):
        reviewer = Mock()
        reviewer.get.return_value = {"items": [{"operation_id": "email", "action": "ATTEST_APPLIED",
            "attested_reference": prep.ATTESTED_REFERENCE, "status": "expired", "expires_at": "2020-01-01T00:00:00Z"}]}
        investigator = Mock()
        with patch.object(prep, "propose", return_value={"proposal_id": "fresh"}) as propose:
            self.assertEqual(prep.choose_email_proposal(investigator, reviewer,
                             {"operation_id": "email", "state": "ESCALATED_UNRESOLVED"}), {"proposal_id": "fresh"})
            propose.assert_called_once_with(investigator, "email", reference=prep.ATTESTED_REFERENCE)
            investigator.post.assert_not_called()

    def test_captured_stale_refusal_is_reused_after_checking_its_live_binding(self):
        trace = {"proposal_id": "old-order-proposal", "http_status": 409,
                 "code": "stale_approval", "reason": "evidence_changed"}
        saved = {"schema_version": 1, "order": {"operation_id": "order", "proposal_id": "old-order-proposal"},
                 "stale_refusal": trace}
        reviewer = Mock()
        reviewer.get.return_value = {"operation_id": "order", "action": "ATTEST_APPLIED", "decision": None,
            "binding": {"seq": 4, "evidence_digest": "old"}, "current": {"seq": 4, "evidence_digest": "new"}}
        investigator = Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ui/public").mkdir(parents=True)
            (root / "ui/public/demo-cases.json").write_text(json.dumps(saved), encoding="utf-8")
            with patch.object(prep, "ROOT", root), patch.object(prep, "propose") as propose:
                self.assertEqual(prep.capture_stale(investigator, reviewer, {"operation_id": "order"}),
                                 ("old-order-proposal", trace))
                reviewer.get.assert_called_once_with("/proposals/old-order-proposal")
                reviewer.post.assert_not_called()
                propose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
