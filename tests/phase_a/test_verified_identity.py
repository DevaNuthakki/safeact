"""Offline RSA signature and identity-security tests."""

import json
import time
import unittest

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from core.verified_identity import (
    IdentityVerificationError,
    IdentityVerifier,
    VerifiedPrincipal,
)


ISSUER = "https://id.safeact.example"
AUDIENCE = "safeact-admission"


class IdentityVerificationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.key_a = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        cls.key_b = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        cls.attacker_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        cls.jwk_a = cls.make_jwk(cls.key_a, "key-a")
        cls.jwk_b = cls.make_jwk(cls.key_b, "key-b")

    @staticmethod
    def make_jwk(key, kid):
        result = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())
        )
        result.update(kid=kid, alg="RS256", use="sig")
        return result

    def setUp(self):
        self.verifier = IdentityVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            public_jwks=(self.jwk_a,),
        )
        now = int(time.time())
        self.claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "subject-123",
            "iat": now - 20,
            "nbf": now - 20,
            "exp": now + 600,
        }

    def sign(
        self, claims=None, *,
        key=None, kid="key-a", headers=None
    ):
        extra = {"kid": kid}
        if headers:
            extra.update(headers)
        return jwt.encode(
            self.claims if claims is None else claims,
            self.key_a if key is None else key,
            algorithm="RS256",
            headers=extra,
        )

    def denied(self, token):
        with self.assertRaises(IdentityVerificationError):
            self.verifier.verify(token)

    def test_valid_signed_token(self):
        result = self.verifier.verify(self.sign())
        self.assertIsInstance(result, VerifiedPrincipal)
        self.assertEqual(result.subject, "subject-123")
        self.assertEqual(result.issuer, ISSUER)
        self.assertEqual(result.audience, AUDIENCE)

    def test_signed_tenant_roles_are_not_permissions(self):
        claims = {
            **self.claims,
            "tenant_id": "forged-tenant",
            "actor_id": "forged-actor",
            "roles": ["admin"],
        }
        result = self.verifier.verify(self.sign(claims))
        self.assertFalse(hasattr(result, "tenant_id"))
        self.assertFalse(hasattr(result, "actor_id"))
        self.assertFalse(hasattr(result, "roles"))

    def test_unknown_key_id(self):
        self.denied(self.sign(kid="unknown"))

    def test_missing_key_id(self):
        self.denied(jwt.encode(
            self.claims, self.key_a, algorithm="RS256"
        ))

    def test_none_algorithm(self):
        self.denied(jwt.encode(
            self.claims, "", algorithm="none",
            headers={"kid": "key-a"}
        ))

    def test_hmac_confusion(self):
        self.denied(jwt.encode(
            self.claims, "attacker-secret" * 3,
            algorithm="HS256", headers={"kid": "key-a"}
        ))

    def test_wrong_issuer(self):
        self.denied(self.sign({
            **self.claims, "iss": "https://evil.example"
        }))

    def test_wrong_audience(self):
        self.denied(self.sign({
            **self.claims, "aud": "other-application"
        }))

    def test_expired_token(self):
        now = int(time.time())
        self.denied(self.sign({
            **self.claims,
            "iat": now - 100,
            "nbf": now - 100,
            "exp": now - 1,
        }))

    def test_future_not_before(self):
        self.denied(self.sign({
            **self.claims, "nbf": int(time.time()) + 300
        }))

    def test_future_issued_at(self):
        self.denied(self.sign({
            **self.claims, "iat": int(time.time()) + 300
        }))

    def test_missing_required_claims(self):
        for name in ("iss", "aud", "sub", "iat", "nbf", "exp"):
            with self.subTest(name=name):
                self.denied(self.sign({
                    key: value
                    for key, value in self.claims.items()
                    if key != name
                }))

    def test_invalid_subjects(self):
        for value in (
            "", " ", " attacker ", "a\n", 123, None, "x" * 256
        ):
            with self.subTest(value=str(value)[:15]):
                self.denied(self.sign({
                    **self.claims, "sub": value
                }))

    def test_noninteger_time_claims(self):
        for name, value in (
            ("exp", True),
            ("exp", float(self.claims["exp"])),
            ("iat", False),
            ("nbf", "0"),
        ):
            with self.subTest(name=name, value=value):
                self.denied(self.sign({
                    **self.claims, name: value
                }))

    def test_excessive_lifetime(self):
        self.denied(self.sign({
            **self.claims,
            "exp": self.claims["iat"] + 3601
        }))

    def test_multiple_audiences_denied(self):
        self.denied(self.sign({
            **self.claims,
            "aud": [AUDIENCE, "another-app"]
        }))

    def test_wrong_signature(self):
        self.denied(self.sign(key=self.attacker_key))

    def test_malformed_token(self):
        for token in (
            "", "abc", "x.y.z", 42, None
        ):
            with self.subTest(token=str(token)):
                self.denied(token)

    def test_oversized_token(self):
        self.denied("a" * 8193)

    def test_remote_key_headers(self):
        for name, value in (
            ("jku", "https://attacker.example/jwks"),
            ("jwk", self.jwk_a),
            ("x5u", "https://attacker.example/cert"),
        ):
            with self.subTest(header=name):
                self.denied(self.sign(
                    headers={name: value}
                ))

    def test_critical_header(self):
        self.denied(self.sign(headers={
            "crit": ["custom"],
            "custom": True,
        }))

    def test_duplicate_trusted_key_id(self):
        with self.assertRaises(IdentityVerificationError):
            IdentityVerifier(
                issuer=ISSUER, audience=AUDIENCE,
                public_jwks=(self.jwk_a, self.jwk_a),
            )

    def test_private_key_in_trust_configuration(self):
        private = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(self.key_a)
        )
        private.update(
            kid="key-a", alg="RS256", use="sig"
        )
        with self.assertRaises(IdentityVerificationError):
            IdentityVerifier(
                issuer=ISSUER, audience=AUDIENCE,
                public_jwks=(private,),
            )

    def test_weak_rsa_key(self):
        weak = rsa.generate_private_key(
            public_exponent=65537, key_size=1024
        )
        with self.assertRaises(IdentityVerificationError):
            IdentityVerifier(
                issuer=ISSUER, audience=AUDIENCE,
                public_jwks=(self.make_jwk(weak, "weak"),),
            )

    def test_key_rotation(self):
        verifier = IdentityVerifier(
            issuer=ISSUER, audience=AUDIENCE,
            public_jwks=(self.jwk_a, self.jwk_b),
        )
        self.assertEqual(
            verifier.verify(self.sign()).subject,
            "subject-123",
        )
        self.assertEqual(
            verifier.verify(self.sign(
                key=self.key_b, kid="key-b"
            )).subject,
            "subject-123",
        )

    def test_bad_verifier_configuration(self):
        for change in (
            {"issuer": ""},
            {"audience": " "},
            {"public_jwks": ()},
            {"max_lifetime_seconds": 0},
            {"max_lifetime_seconds": True},
        ):
            with self.subTest(change=change):
                params = {
                    "issuer": ISSUER,
                    "audience": AUDIENCE,
                    "public_jwks": (self.jwk_a,),
                }
                params.update(change)
                with self.assertRaises(
                    IdentityVerificationError
                ):
                    IdentityVerifier(**params)


if __name__ == "__main__":
    unittest.main()
