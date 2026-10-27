"""Pure, database-independent HMAC proof contract tests."""

import hmac
import json
import time
import unittest
from dataclasses import replace
from hashlib import sha256

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from psycopg import sql

from core.admission_authorization import RequestedIntent
from core.verified_identity import IdentityVerifier, IdentityVerificationError
from storage.disposable_hmac_proof import (
    ARGUMENT_FIELDS,
    SIGNED_FIELDS,
    encoded_part,
    issue_verified_proof,
    proof_message,
    proof_parameters,
    proof_part_sql,
    protected_gate_sql,
    proof_call_sql,
)


class DisposableHmacProofTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        jwk = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(
                cls.private_key.public_key()
            )
        )
        jwk.update(kid="test-rsa",alg="RS256",use="sig")
        cls.verifier = IdentityVerifier(
            issuer="https://id.example",
            audience="safeact-admission",
            public_jwks=(jwk,),
        )

    def token(self, *, subject="subject-a", signing_key=None):
        now = int(time.time())
        return jwt.encode(
            {
                "iss":"https://id.example",
                "aud":"safeact-admission",
                "sub":subject,
                "iat":now-10,
                "nbf":now-10,
                "exp":now+600,
            },
            self.private_key if signing_key is None else signing_key,
            algorithm="RS256",
            headers={"kid":"test-rsa"},
        )

    def request(self, payload=None):
        return RequestedIntent(
            request_key="request-001",
            action_type="create_case",
            resource_id="resource-a",
            provider_account_id="account-a",
            connector_id="connector-a",
            contract_version="v1",
            payload={"z":2,"a":1} if payload is None else payload,
        )

    def issue(self, **kwargs):
        return issue_verified_proof(
            identity_verifier=self.verifier,
            token=self.token(),
            request=self.request(),
            signing_key=b"k"*32,
            **kwargs,
        )

    def test_disposable_only_sql_builders(self):
        for builder in (
            proof_part_sql, protected_gate_sql, proof_call_sql
        ):
            with self.subTest(builder=builder.__name__):
                with self.assertRaises(ValueError):
                    builder("public")
                self.assertIsInstance(
                    builder("safeact_it_"+"a"*32),
                    sql.Composable,
                )

    def test_explicit_null_and_byte_length_encoding(self):
        self.assertEqual(encoded_part(None),"-1:")
        self.assertEqual(encoded_part(""),"0:")
        self.assertEqual(encoded_part("é"),"2:c3a9")

    def test_proof_has_exact_named_fields(self):
        proof = self.issue(nonce="a"*32)
        self.assertEqual(len(ARGUMENT_FIELDS),16)
        self.assertEqual(len(SIGNED_FIELDS),16)
        self.assertEqual(
            set(proof),set(ARGUMENT_FIELDS)|{"domain"}
        )
        self.assertEqual(len(proof_parameters(proof)),16)
        self.assertEqual(len(proof["mac"]),32)

    def test_every_field_is_bound_to_mac(self):
        proof = self.issue(nonce="a"*32)
        mutations = {
            "issuer":"https://other.example",
            "subject":"subject-b",
            "request_key":"changed",
            "action_type":"delete_case",
            "resource_id":"changed-resource",
            "connector_id":"changed-connector",
            "provider_account_id":"changed-account",
            "contract_version":"v2",
            "payload_canonical":'{"a":2}',
            "payload_sha256":"0"*64,
            "provider_idempotency_key":"changed",
            "proof_key_id":"other-key",
            "issued_at":proof["issued_at"]+1,
            "expires_at":proof["expires_at"]+1,
            "nonce":"b"*32,
        }
        self.assertEqual(
            set(mutations),set(SIGNED_FIELDS)-{"domain"}
        )
        for field, value in mutations.items():
            changed = dict(proof)
            changed[field] = value
            with self.subTest(field=field):
                self.assertNotEqual(
                    proof_message(changed),
                    proof_message(proof),
                )
                self.assertFalse(hmac.compare_digest(
                    hmac.new(
                        b"k"*32,proof_message(changed),sha256
                    ).digest(),
                    proof["mac"],
                ))

    def test_invalid_jwt_cannot_receive_proof(self):
        key = rsa.generate_private_key(
            public_exponent=65537,key_size=2048
        )
        with self.assertRaises(IdentityVerificationError):
            issue_verified_proof(
                identity_verifier=self.verifier,
                token=self.token(signing_key=key),
                request=self.request(),
                signing_key=b"k"*32,
            )

    def test_canonical_payload_order_is_stable(self):
        common = dict(
            identity_verifier=self.verifier,
            token=self.token(),
            signing_key=b"k"*32,
            issued_at=1800000000,
            expires_at=1800000060,
            nonce="a"*32,
        )
        first = issue_verified_proof(
            request=self.request({"z":2,"a":1}),**common
        )
        second = issue_verified_proof(
            request=self.request({"a":1,"z":2}),**common
        )
        self.assertEqual(first["payload_canonical"],
                         second["payload_canonical"])
        self.assertEqual(first["mac"],second["mac"])


if __name__ == "__main__":
    unittest.main()
