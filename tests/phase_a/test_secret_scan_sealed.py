"""The sealed-evidence allowlist is exact: one file, five digests."""

import hashlib
import tempfile
import unittest

from pathlib import Path

from deploy.evaluation.agent_v1 import secret_scan


ROOT = Path(__file__).resolve().parents[2]
SEALED = "docs/phase-d/stripe-sandbox-v1/REVIEW-M58.md"
# The file as committed in 3269385; it must never be edited.
SEALED_SHA256 = "c2aa534430785f9326a4db7b3571e23e5731b204895be270048df581a7d47c4d"


class SealedDigestAllowlistTests(unittest.TestCase):

    def test_sealed_review_is_unchanged(self):
        data = (ROOT / SEALED).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), SEALED_SHA256)

    def test_sealed_review_scans_clean(self):
        self.assertEqual(secret_scan.scan([ROOT / SEALED]), {})

    def test_every_allowed_value_occurs_in_the_sealed_review(self):
        text = (ROOT / SEALED).read_text()
        self.assertEqual(set(secret_scan.SEALED_DIGESTS), {SEALED})
        for value in secret_scan.SEALED_DIGESTS[SEALED]:
            self.assertIn(value, text)

    def test_the_same_values_are_flagged_in_any_other_file(self):
        values = sorted(secret_scan.SEALED_DIGESTS[SEALED])
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp) / "REVIEW-M58.md"
            other.write_text("\n".join(values) + "\n")
            found = secret_scan.scan([other])
        self.assertEqual(len(found[str(other)]), len(values))

    def test_another_value_in_the_sealed_file_is_still_flagged(self):
        text = (ROOT / SEALED).read_text() + "\n" + "ab" * 32 + "\n"
        found = secret_scan.scan_text(
            text, suffix=".md",
            sealed=secret_scan._sealed_digests(ROOT / SEALED))
        self.assertEqual(found, [(text.count("\n"), "hex64_secret_shape")])

    def test_key_shapes_are_never_allowlisted(self):
        line = "sk-" + "a" * 20
        found = secret_scan.scan_text(
            line, suffix=".md",
            sealed=secret_scan._sealed_digests(ROOT / SEALED))
        self.assertEqual(found, [(1, "openai_key")])


if __name__ == "__main__":
    unittest.main()
