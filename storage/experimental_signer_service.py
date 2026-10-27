"""Disposable-only authenticated proof signer test service.

Never deploy as a production signer. Its bearer authentication
and HTTP transport are intentionally limited to an isolated
integration fixture.
"""

import hmac
import json
import os

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core.admission_authorization import RequestedIntent
from core.verified_identity import (
    IdentityVerifier,
    IdentityVerificationError,
)
from storage.disposable_hmac_proof import issue_verified_proof


MAX_BODY = 8192

REQUEST_FIELDS = frozenset({
    "request_key",
    "action_type",
    "resource_id",
    "provider_account_id",
    "connector_id",
    "contract_version",
    "payload",
    "provider_idempotency_key",
})


@dataclass(frozen=True, slots=True)
class SignerRuntime:
    verifier: IdentityVerifier = field(repr=False)
    signing_key: bytes = field(repr=False)
    service_token: str = field(repr=False)

    def __post_init__(self):
        if type(self.verifier) is not IdentityVerifier:
            raise TypeError(
                "Trusted IdentityVerifier required"
            )

        if (
            type(self.signing_key) is not bytes
            or len(self.signing_key) != 32
        ):
            raise ValueError(
                "32-byte disposable signing key required"
            )

        if (
            type(self.service_token) is not str
            or len(self.service_token) < 32
        ):
            raise ValueError(
                "Strong disposable service token required"
            )


def _unique_object(pairs):
    result = {}

    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON property")

        result[name] = value

    return result


def _reject_constant(_value):
    raise ValueError("Non-finite JSON value")


def parse_envelope(raw):
    if (
        type(raw) is not bytes
        or not 2 <= len(raw) <= MAX_BODY
    ):
        raise ValueError("Invalid request size")

    envelope = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )

    if (
        type(envelope) is not dict
        or set(envelope) != {"token", "request"}
    ):
        raise ValueError("Invalid signing envelope")

    token = envelope["token"]
    request = envelope["request"]

    if (
        type(token) is not str
        or type(request) is not dict
    ):
        raise ValueError(
            "Invalid token/request types"
        )

    if set(request) != REQUEST_FIELDS:
        raise ValueError(
            "Unexpected request fields"
        )

    return token, RequestedIntent(**request)


def issue(runtime, raw):
    if type(runtime) is not SignerRuntime:
        raise TypeError("Invalid signer runtime")

    token, request = parse_envelope(raw)

    proof = issue_verified_proof(
        identity_verifier=runtime.verifier,
        token=token,
        request=request,
        signing_key=runtime.signing_key,
    )

    return {
        **proof,
        "mac": proof["mac"].hex(),
    }


def handler_for(runtime):
    if type(runtime) is not SignerRuntime:
        raise TypeError("Invalid signer runtime")

    class Handler(BaseHTTPRequestHandler):

        def log_message(self, *_args):
            return

        def log_error(self, *_args):
            return

        def _respond(self, status, result):
            data = json.dumps(
                result,
                separators=(",", ":"),
            ).encode("utf-8")

            self.send_response(status)
            self.send_header(
                "Content-Type",
                "application/json",
            )
            self.send_header(
                "Content-Length",
                str(len(data)),
            )
            self.send_header(
                "Connection",
                "close",
            )
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            return hmac.compare_digest(
                self.headers.get(
                    "Authorization", ""
                ),
                "Bearer " + runtime.service_token,
            )

        def do_GET(self):
            if not self._authorized():
                return self._respond(
                    403, {"error": "denied"}
                )

            if self.path != "/ready":
                return self._respond(
                    404, {"error": "not_found"}
                )

            return self._respond(
                200, {"ready": True}
            )

        def do_POST(self):
            if not self._authorized():
                return self._respond(
                    403, {"error": "denied"}
                )

            if self.path != "/issue":
                return self._respond(
                    404, {"error": "not_found"}
                )

            size = self.headers.get(
                "Content-Length", ""
            )

            if (
                not size.isascii()
                or not size.isdecimal()
                or not 2 <= int(size) <= MAX_BODY
                or self.headers.get(
                    "Transfer-Encoding"
                ) is not None
            ):
                return self._respond(
                    400, {"error": "invalid_request"}
                )

            try:
                proof = issue(
                    runtime,
                    self.rfile.read(int(size)),
                )

            except IdentityVerificationError:
                return self._respond(
                    403, {"error": "identity_denied"}
                )

            except (
                ValueError,
                TypeError,
                KeyError,
                UnicodeError,
            ):
                return self._respond(
                    400, {"error": "invalid_request"}
                )

            return self._respond(
                200, {"proof": proof}
            )

    return Handler


def create_server(
    runtime,
    host="127.0.0.1",
    port=0,
):
    return ThreadingHTTPServer(
        (host, port),
        handler_for(runtime),
    )


def main():
    if (
        os.environ.get(
            "SAFEACT_RUN_DISPOSABLE_SIGNER"
        ) != "1"
    ):
        raise RuntimeError(
            "Disposable signer explicit opt-in required"
        )

    if os.geteuid() != 12001:
        raise RuntimeError(
            "Disposable signer requires isolated non-root UID"
        )

    if Path(
        "/run/admission/login.json"
    ).exists():
        raise RuntimeError(
            "Admission DB credential exposed to signer"
        )

    key = Path(
        "/run/signer/hmac.key"
    ).read_bytes()

    token = Path(
        "/run/auth/service-token"
    ).read_text(
        encoding="utf-8"
    ).strip()

    jwks = json.loads(
        Path(
            "/run/signer/jwks.json"
        ).read_text(encoding="utf-8")
    )

    if (
        type(jwks) is not dict
        or type(jwks.get("keys")) is not list
    ):
        raise RuntimeError(
            "Invalid trusted JWK file"
        )

    verifier = IdentityVerifier(
        issuer="https://id.example",
        audience="safeact-admission",
        public_jwks=tuple(jwks["keys"]),
    )

    runtime = SignerRuntime(
        verifier,
        key,
        token,
    )

    with create_server(
        runtime,
        host="0.0.0.0",
        port=8776,
    ) as server:
        server.serve_forever(
            poll_interval=0.2
        )


if __name__ == "__main__":
    main()
