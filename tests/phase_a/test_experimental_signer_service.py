"""Tests for the disposable-only HTTP signing boundary."""

import hashlib
import hmac
import json
import threading
import time
import unittest
import urllib.error
import urllib.request

import jwt

from cryptography.hazmat.primitives.asymmetric import rsa

from core.verified_identity import IdentityVerifier
from storage.disposable_hmac_proof import proof_message
from storage.experimental_signer_service import (
    MAX_BODY,
    SignerRuntime,
    create_server,
    issue,
    parse_envelope,
)
from storage.submission import _prepare_payload


REQUEST = {
    "request_key": "signer-regression-001",
    "action_type": "create_case",
    "resource_id": "resource-a",
    "provider_account_id": "account-a",
    "connector_id": "connector-a",
    "contract_version": "v1",
    "payload": {
        "case_id": "resource-a",
    },
    "provider_idempotency_key": None,
}


class DisposableSignerServiceTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.rsa = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        jwk = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(
                cls.rsa.public_key()
            )
        )

        jwk.update(
            kid="disposable-signer-test",
            alg="RS256",
            use="sig",
        )

        cls.signing_key = b"S" * 32
        cls.service_token = "s" * 48

        cls.runtime = SignerRuntime(
            verifier=IdentityVerifier(
                issuer="https://id.example",
                audience="safeact-admission",
                public_jwks=(jwk,),
            ),
            signing_key=cls.signing_key,
            service_token=cls.service_token,
        )

        cls.server = create_server(
            cls.runtime
        )

        cls.thread = threading.Thread(
            target=cls.server.serve_forever,
            daemon=True,
        )

        cls.thread.start()

        cls.base = (
            f"http://127.0.0.1:"
            f"{cls.server.server_port}"
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

        cls.thread.join(timeout=3)

        if cls.thread.is_alive():
            raise RuntimeError(
                "Disposable signer test server did not stop"
            )

    def token(self, subject="subject-a"):
        now = int(time.time())

        return jwt.encode(
            {
                "iss": "https://id.example",
                "aud": "safeact-admission",
                "sub": subject,
                "iat": now - 10,
                "nbf": now - 10,
                "exp": now + 600,
            },
            self.rsa,
            algorithm="RS256",
            headers={
                "kid": "disposable-signer-test",
            },
        )

    def envelope(self, request=None, token=None):
        return {
            "token": (
                self.token()
                if token is None
                else token
            ),
            "request": (
                REQUEST
                if request is None
                else request
            ),
        }

    def request(
        self,
        path="/issue",
        data=None,
        auth=True,
        method="POST",
    ):
        headers = {}

        if auth is True:
            headers["Authorization"] = (
                "Bearer " + self.service_token
            )

        elif type(auth) is str:
            headers["Authorization"] = (
                "Bearer " + auth
            )

        if data is not None:
            if type(data) is not bytes:
                data = json.dumps(
                    data
                ).encode("utf-8")

            headers["Content-Type"] = (
                "application/json"
            )

        req = urllib.request.Request(
            self.base + path,
            data=data,
            headers=headers,
            method=method,
        )

        try:
            with urllib.request.urlopen(
                req,
                timeout=5,
            ) as response:
                return (
                    response.status,
                    json.loads(
                        response.read()
                    ),
                )

        except urllib.error.HTTPError as exc:
            return (
                exc.code,
                json.loads(exc.read()),
            )

    def test_valid_proof_matches_existing_contract(self):
        status, body = self.request(
            data=self.envelope()
        )

        self.assertEqual(status, 200)

        proof = body["proof"]

        self.assertEqual(
            proof["subject"],
            "subject-a",
        )

        self.assertEqual(
            proof["issuer"],
            "https://id.example",
        )

        self.assertEqual(
            proof["request_key"],
            REQUEST["request_key"],
        )

        self.assertEqual(
            proof["domain"],
            "safeact-admission-proof-v1",
        )

        canonical, digest = _prepare_payload(
            REQUEST["payload"]
        )

        self.assertEqual(
            proof["payload_canonical"],
            canonical,
        )

        self.assertEqual(
            proof["payload_sha256"],
            digest,
        )

        actual_mac = bytes.fromhex(
            proof["mac"]
        )

        expected_mac = hmac.new(
            self.signing_key,
            proof_message({
                **proof,
                "mac": actual_mac,
            }),
            hashlib.sha256,
        ).digest()

        self.assertTrue(
            hmac.compare_digest(
                actual_mac,
                expected_mac,
            )
        )

    def test_service_authentication_denials(self):
        self.assertEqual(
            self.request(
                data=self.envelope(),
                auth=False,
            )[0],
            403,
        )

        self.assertEqual(
            self.request(
                data=self.envelope(),
                auth="wrong",
            )[0],
            403,
        )

    def test_invalid_jwt_denied(self):
        self.assertEqual(
            self.request(
                data=self.envelope(
                    token="not.a.jwt"
                )
            )[0],
            403,
        )

    def test_caller_identity_fields_denied(self):
        for name in (
            "subject",
            "actor_id",
            "tenant_id",
            "roles",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    self.request(
                        data=self.envelope({
                            **REQUEST,
                            name: "forged",
                        })
                    )[0],
                    400,
                )

    def test_duplicate_json_fields_denied(self):
        raw = (
            '{"token":"x","token":"y","request":{}}'
        ).encode()

        with self.assertRaises(ValueError):
            parse_envelope(raw)

        self.assertEqual(
            self.request(data=raw)[0],
            400,
        )

    def test_nonfinite_json_denied(self):
        raw = (
            b'{"token":"x","request":{"payload":NaN}}'
        )

        with self.assertRaises(ValueError):
            parse_envelope(raw)

        self.assertEqual(
            self.request(data=raw)[0],
            400,
        )

    def test_extra_envelope_field_denied(self):
        self.assertEqual(
            self.request(
                data={
                    **self.envelope(),
                    "subject": "forged",
                }
            )[0],
            400,
        )

    def test_large_request_denied(self):
        oversized = (
            b"{" + b" " * MAX_BODY + b"}"
        )

        self.assertEqual(
            self.request(data=oversized)[0],
            400,
        )

    def test_authenticated_readiness_and_unknown_routes(self):
        self.assertEqual(
            self.request(
                "/ready",
                method="GET",
            )[0],
            200,
        )

        self.assertEqual(
            self.request(
                "/ready",
                method="GET",
                auth=False,
            )[0],
            403,
        )

        self.assertEqual(
            self.request(
                "/not-a-route",
                method="GET",
            )[0],
            404,
        )

        self.assertEqual(
            self.request(
                "/not-a-route",
                data=self.envelope(),
            )[0],
            404,
        )

    def test_runtime_rejects_weak_keys_and_redacts_secret_repr(self):
        self.assertNotIn(
            self.service_token,
            repr(self.runtime),
        )

        self.assertNotIn(
            self.signing_key.hex(),
            repr(self.runtime),
        )

        with self.assertRaises(ValueError):
            SignerRuntime(
                self.runtime.verifier,
                b"short",
                self.service_token,
            )

        with self.assertRaises(ValueError):
            SignerRuntime(
                self.runtime.verifier,
                self.signing_key,
                "short",
            )

    def test_pure_signer_returns_protocol_mac_as_hex(self):
        raw = json.dumps(
            self.envelope()
        ).encode()

        proof = issue(
            self.runtime,
            raw,
        )

        self.assertEqual(
            len(proof["mac"]),
            64,
        )

        self.assertEqual(
            proof["subject"],
            "subject-a",
        )


if __name__ == "__main__":
    unittest.main()
