"""Tests for SafeAct's shared canonical UTF-8 representation."""

import hashlib
import json
import unittest

from core.contracts import (
    canonical_payload_bytes,
    canonical_payload_hash,
)


class CanonicalPayloadBytesTests(unittest.TestCase):

    def test_unicode_bytes_match_storage_format(self):
        payload = {
            "name": "café",
            "count": 1,
        }

        expected = (
            '{"count":1,"name":"café"}'
        ).encode("utf-8")

        self.assertEqual(
            canonical_payload_bytes(payload),
            expected,
        )

    def test_reordered_keys_produce_identical_bytes(self):
        first = {"z": 3, "a": 1}
        second = {"a": 1, "z": 3}

        self.assertEqual(
            canonical_payload_bytes(first),
            canonical_payload_bytes(second),
        )
        self.assertEqual(
            canonical_payload_hash(first),
            canonical_payload_hash(second),
        )

    def test_nested_structures_preserve_meaning(self):
        payload = {
            "outer": {
                "tags": ["a", None, True],
                "count": 7,
            },
        }

        wire = canonical_payload_bytes(payload)

        self.assertEqual(
            json.loads(wire.decode("utf-8")),
            payload,
        )

        self.assertEqual(
            canonical_payload_hash(payload),
            hashlib.sha256(wire).hexdigest(),
        )

    def test_boolean_is_not_integer_one(self):
        boolean_wire = canonical_payload_bytes(
            {"value": True}
        )

        integer_wire = canonical_payload_bytes(
            {"value": 1}
        )

        self.assertNotEqual(
            boolean_wire,
            integer_wire,
        )

    def test_floating_point_payload_is_rejected(self):
        with self.assertRaises(ValueError):
            canonical_payload_bytes(
                {"amount": 1.25}
            )

        with self.assertRaises(ValueError):
            canonical_payload_hash(
                {"amount": 1.25}
            )

    def test_root_must_be_json_object(self):
        for payload in (
            [],
            None,
            "string",
            42,
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    canonical_payload_bytes(payload)

    def test_object_keys_must_be_strings(self):
        with self.assertRaises(ValueError):
            canonical_payload_bytes({
                1: "integer-key",
            })

        with self.assertRaises(ValueError):
            canonical_payload_bytes({
                "nested": {
                    2: "integer-key",
                },
            })

    def test_invalid_unicode_is_rejected(self):
        payload = {
            "text": "\\ud800",
        }

        # A lone surrogate cannot become valid UTF-8.
        # This string is built without embedding an
        # invalid source-file encoding.
        payload["text"] = chr(0xD800)

        with self.assertRaises(UnicodeError):
            canonical_payload_bytes(payload)


if __name__ == "__main__":
    unittest.main()
