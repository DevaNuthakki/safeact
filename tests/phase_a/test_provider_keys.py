"""Server-side provider key derivation. No database."""

import unittest

from hashlib import sha256

from core.provider_keys import KEY_PREFIX, derive_provider_key
from storage.lifecycle_sql import v0008


IDENTITY = ("tenant-a", "request-1", "mock-payments", "acct-1")


class ProviderKeyTests(unittest.TestCase):

    def test_key_is_a_pure_function_of_operation_identity(self):
        self.assertEqual(
            derive_provider_key(*IDENTITY), derive_provider_key(*IDENTITY)
        )
        self.assertTrue(derive_provider_key(*IDENTITY).startswith(KEY_PREFIX))

    def test_distinct_identities_get_distinct_keys(self):
        keys = {
            derive_provider_key(*IDENTITY),
            derive_provider_key("tenant-b", *IDENTITY[1:]),
            derive_provider_key(IDENTITY[0], "request-2", *IDENTITY[2:]),
            derive_provider_key(*IDENTITY[:3], "acct-2"),
            # Field boundaries cannot be shifted to collide.
            derive_provider_key("tenant-ar", "equest-1", *IDENTITY[2:]),
        }
        self.assertEqual(len(keys), 5)

    def test_connectors_without_native_keys_get_none(self):
        for connector in ("mock-crm", "mock-email", "unknown"):
            with self.subTest(connector=connector):
                self.assertIsNone(derive_provider_key(
                    IDENTITY[0], IDENTITY[1], connector, IDENTITY[3]
                ))

    def test_sql_check_uses_the_same_derivation(self):
        # The SQL expression joins the same fields with chr(31) and
        # hashes their UTF-8 bytes with sha256, hex encoded.
        self.assertIn("chr(31)", v0008.DERIVED_KEY)
        self.assertIn("'sa1_'", v0008.DERIVED_KEY)
        expected = KEY_PREFIX + sha256(
            "\x1f".join(IDENTITY).encode("utf-8")
        ).hexdigest()
        self.assertEqual(derive_provider_key(*IDENTITY), expected)


if __name__ == "__main__":
    unittest.main()
