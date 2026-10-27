"""PostgreSQL payload-integrity and trust-boundary tests.

All test operations are synthetic and rolled back.

The tests explicitly distinguish database byte/hash
binding from application-level canonicalization.
"""

import hashlib
import json
import os
import unittest
from uuid import uuid4

import psycopg

from core.contracts import canonical_payload_hash
from tests.integration.test_postgres_smoke import (
    database_settings,
)


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1",
    "Explicit PostgreSQL integration opt-in required",
)
class CanonicalPayloadTests(unittest.TestCase):

    def setUp(self):
        self.db = psycopg.connect(**database_settings())

        if self.db.autocommit:
            self.db.close()
            raise RuntimeError(
                "Payload tests must use rollback-capable transactions"
            )

        # Ensure nested transaction contexts use savepoints.
        self.db.execute("SELECT 1")

    def tearDown(self):
        try:
            self.db.rollback()
        finally:
            self.db.close()

    @staticmethod
    def canonical_wire(payload):
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    @staticmethod
    def digest_of(wire):
        return hashlib.sha256(
            wire.encode("utf-8")
        ).hexdigest()

    def insert(self, wire, *, digest=None):
        operation_id = uuid4()

        if digest is None:
            digest = (
                self.digest_of(wire)
                if wire is not None
                else "0" * 64
            )

        self.db.execute(
            """
            INSERT INTO public.safeact_operations (
                operation_id,
                tenant_id,
                request_key,
                action_type,
                resource_id,
                provider_account_id,
                connector_id,
                contract_version,
                payload_sha256,
                payload_canonical
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s
            )
            """,
            (
                operation_id,
                "synthetic-tenant",
                uuid4().hex,
                "create_case",
                "synthetic-resource",
                "mock-provider-account",
                "mock-crm",
                "v1",
                digest,
                wire,
            ),
        )

        return operation_id

    def reject(self, operation, expected_sqlstate=None):
        # Outer connection is already in a transaction.
        # A nested transaction rolls back failed statements
        # without poisoning the enclosing test transaction.
        with self.assertRaises(psycopg.Error) as caught:
            with self.db.transaction():
                operation()

        if expected_sqlstate is not None:
            self.assertEqual(
                caught.exception.sqlstate,
                expected_sqlstate,
            )

    def test_unicode_round_trip_matches_phase_a(self):
        payload = {
            "name": "café",
            "nested": {"z": 9, "a": True},
            "tags": ["synthetic", None],
        }

        wire = self.canonical_wire(payload)
        digest = canonical_payload_hash(payload)

        operation_id = self.insert(
            wire,
            digest=digest,
        )

        actual = self.db.execute(
            """
            SELECT payload_canonical, payload_sha256
            FROM public.safeact_operations
            WHERE operation_id = %s
            """,
            (operation_id,),
        ).fetchone()

        self.assertEqual(actual, (wire, digest))
        self.assertEqual(json.loads(actual[0]), payload)

    def test_key_order_does_not_change_canonical_digest(self):
        first = {"b": 2, "a": 1}
        second = {"a": 1, "b": 2}

        first_wire = self.canonical_wire(first)
        second_wire = self.canonical_wire(second)

        self.assertEqual(first_wire, second_wire)
        self.assertEqual(
            canonical_payload_hash(first),
            canonical_payload_hash(second),
        )

        first_id = self.insert(first_wire)
        second_id = self.insert(second_wire)

        self.assertNotEqual(first_id, second_id)

    def test_wrong_digest_is_rejected(self):
        wire = self.canonical_wire({"amount": 10})

        self.reject(
            lambda: self.insert(
                wire,
                digest="0" * 64,
            ),
            "23514",
        )

    def test_array_payload_is_rejected(self):
        self.reject(
            lambda: self.insert("[1,2,3]"),
            "23514",
        )

    def test_malformed_json_is_rejected(self):
        self.reject(
            lambda: self.insert('{"broken":')
        )

    def test_null_payload_is_rejected(self):
        self.reject(
            lambda: self.insert(None),
            "23502",
        )

    def test_payload_above_maximum_size_is_rejected(self):
        wire = self.canonical_wire(
            {"blob": "x" * 65536}
        )

        self.assertGreater(
            len(wire.encode("utf-8")),
            65536,
        )

        self.reject(
            lambda: self.insert(wire),
            "23514",
        )

    def test_exact_maximum_size_is_allowed(self):
        empty_wire = self.canonical_wire(
            {"blob": ""}
        )

        allowed_chars = (
            65536 - len(empty_wire.encode("utf-8"))
        )

        wire = self.canonical_wire(
            {"blob": "x" * allowed_chars}
        )

        self.assertEqual(
            len(wire.encode("utf-8")),
            65536,
        )

        operation_id = self.insert(wire)

        length = self.db.execute(
            """
            SELECT octet_length(payload_canonical)
            FROM public.safeact_operations
            WHERE operation_id = %s
            """,
            (operation_id,),
        ).fetchone()[0]

        self.assertEqual(length, 65536)

    def test_payload_update_is_rejected(self):
        operation_id = self.insert(
            self.canonical_wire({"safe": True})
        )

        self.reject(
            lambda: self.db.execute(
                """
                UPDATE public.safeact_operations
                SET payload_canonical = %s
                WHERE operation_id = %s
                """,
                (
                    self.canonical_wire({"safe": False}),
                    operation_id,
                ),
            ),
            "23514",
        )

    def test_digest_update_is_rejected(self):
        operation_id = self.insert(
            self.canonical_wire({"safe": True})
        )

        self.reject(
            lambda: self.db.execute(
                """
                UPDATE public.safeact_operations
                SET payload_sha256 = payload_sha256
                WHERE operation_id = %s
                """,
                (operation_id,),
            ),
            "23514",
        )

    def test_database_accepts_noncanonical_spacing(self):
        # Known limitation: PostgreSQL binds raw bytes to
        # their hash, but does not require canonical spacing.
        payload = {"b": 2, "a": 1}
        noncanonical = '{"b":2, "a":1}'

        self.assertNotEqual(
            noncanonical,
            self.canonical_wire(payload),
        )

        self.assertNotEqual(
            self.digest_of(noncanonical),
            canonical_payload_hash(payload),
        )

        operation_id = self.insert(noncanonical)

        self.assertIsInstance(operation_id, type(uuid4()))

    def test_database_accepts_duplicate_json_keys(self):
        # Known limitation: JSONB parsing accepts duplicate
        # keys, unlike the required future HTTP admission rule.
        noncanonical = '{"case":1,"case":2}'

        operation_id = self.insert(noncanonical)

        stored = self.db.execute(
            """
            SELECT payload_canonical
            FROM public.safeact_operations
            WHERE operation_id = %s
            """,
            (operation_id,),
        ).fetchone()[0]

        self.assertEqual(stored, noncanonical)

    def test_database_accepts_float_rejected_by_phase_a(self):
        # The database's JSON-object check does not enforce
        # the restricted primitive types in Phase A.
        payload = {"ratio": 1.25}

        with self.assertRaises(ValueError):
            canonical_payload_hash(payload)

        wire = self.canonical_wire(payload)
        operation_id = self.insert(wire)

        self.assertIsInstance(operation_id, type(uuid4()))


if __name__ == "__main__":
    unittest.main()
