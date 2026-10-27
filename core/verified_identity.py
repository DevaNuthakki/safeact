"""Offline, trusted-key RS256 identity verification.

No network lookup, token signing, grant resolution, or dispatch.
"""

from dataclasses import dataclass
from typing import Mapping

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey


class IdentityVerificationError(PermissionError):
    """Fail-closed identity verification error."""


@dataclass(frozen=True, slots=True)
class VerifiedPrincipal:
    issuer: str
    subject: str
    audience: str
    issued_at: int
    expires_at: int


class IdentityVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        public_jwks: tuple[Mapping[str, object], ...],
        max_lifetime_seconds: int = 3600,
    ):
        for value in (issuer, audience):
            if (
                type(value) is not str
                or not value
                or len(value) > 512
                or value != value.strip()
                or any(ord(c) < 33 or ord(c) == 127 for c in value)
            ):
                raise IdentityVerificationError(
                    "Invalid identity verifier configuration"
                )

        if (
            type(max_lifetime_seconds) is not int
            or not 1 <= max_lifetime_seconds <= 86400
            or type(public_jwks) is not tuple
            or not 1 <= len(public_jwks) <= 8
        ):
            raise IdentityVerificationError(
                "Invalid identity verifier configuration"
            )

        keys = {}

        for item in public_jwks:
            if not isinstance(item, Mapping):
                raise IdentityVerificationError("Invalid JWK")

            kid = item.get("kid")

            if (
                type(kid) is not str
                or not 1 <= len(kid) <= 128
                or kid != kid.strip()
                or any(ord(c) < 33 or ord(c) == 127 for c in kid)
                or kid in keys
                or item.get("kty") != "RSA"
                or item.get("alg") != "RS256"
                or item.get("use") != "sig"
                or any(
                    name in item
                    for name in ("d", "p", "q", "dp", "dq", "qi", "oth")
                )
                or (
                    "key_ops" in item
                    and item["key_ops"] != ["verify"]
                )
            ):
                raise IdentityVerificationError(
                    "Invalid trusted key metadata"
                )

            try:
                key = jwt.PyJWK.from_dict(
                    dict(item),
                    algorithm="RS256",
                ).key
            except Exception as exc:
                raise IdentityVerificationError(
                    "Invalid trusted RSA public key"
                ) from exc

            if (
                not isinstance(key, RSAPublicKey)
                or key.key_size < 2048
            ):
                raise IdentityVerificationError(
                    "Weak or invalid RSA verification key"
                )

            keys[kid] = key

        self._issuer = issuer
        self._audience = audience
        self._keys = keys
        self._max_lifetime = max_lifetime_seconds

    def verify(self, token: str) -> VerifiedPrincipal:
        if (
            type(token) is not str
            or not 1 <= len(token) <= 8192
        ):
            raise IdentityVerificationError(
                "Invalid identity token"
            )

        try:
            header = jwt.get_unverified_header(token)
        except (jwt.PyJWTError, TypeError, ValueError) as exc:
            raise IdentityVerificationError(
                "Malformed identity token"
            ) from exc

        if (
            type(header) is not dict
            or header.get("alg") != "RS256"
            or type(header.get("kid")) is not str
            or header["kid"] not in self._keys
            or any(
                name in header
                for name in (
                    "jku", "jwk", "x5u", "x5c", "crit", "b64"
                )
            )
        ):
            raise IdentityVerificationError(
                "Untrusted identity token header"
            )

        try:
            claims = jwt.decode(
                token,
                key=self._keys[header["kid"]],
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._audience,
                leeway=0,
                options={
                    "require": [
                        "iss", "aud", "sub", "iat", "nbf", "exp"
                    ]
                },
            )
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            raise IdentityVerificationError(
                "Invalid signature or identity claims"
            ) from exc

        subject = claims.get("sub")

        if (
            type(subject) is not str
            or not 1 <= len(subject) <= 255
            or subject != subject.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in subject)
            or type(claims.get("iss")) is not str
            or type(claims.get("aud")) is not str
            or claims["iss"] != self._issuer
            or claims["aud"] != self._audience
            or any(
                type(claims.get(name)) is not int
                for name in ("iat", "nbf", "exp")
            )
        ):
            raise IdentityVerificationError(
                "Invalid identity claim types"
            )

        if not (
            0 < claims["exp"] - claims["iat"] <= self._max_lifetime
        ):
            raise IdentityVerificationError(
                "Invalid token lifetime"
            )

        return VerifiedPrincipal(
            issuer=self._issuer,
            subject=subject,
            audience=self._audience,
            issued_at=claims["iat"],
            expires_at=claims["exp"],
        )
