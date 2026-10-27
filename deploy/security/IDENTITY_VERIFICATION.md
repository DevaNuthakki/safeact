# SafeAct — Signed identity verification

Status: Offline RS256 identity verifier; real OIDC integration pending.

## Trusted configuration

The verifier requires a server-configured issuer, audience, and trusted
RSA public JWKs. It never downloads keys or follows token-supplied key
locations. Signing keys must use RS256 and have at least 2048 bits.

Unknown key IDs, invalid signatures, wrong issuers, wrong audiences,
missing required claims, invalid time claims, and expired tokens fail
closed. Key rotation is explicit.

## Identity versus authorization

The returned VerifiedPrincipal contains issuer, subject, audience,
issued-at time, and expiry only.

JWT-provided tenant, actor, roles, grants, resource permissions, and
provider account fields are NOT authorization evidence.

A verified issuer/subject pair must be mapped to an internal actor and
tenant through authoritative server-controlled policy.

A Python VerifiedPrincipal object can be constructed by code; it is
not an unforgeable capability. Only the trusted entry point may accept
tokens and call the verifier.

## Limitations

- No real external identity provider has been integrated.
- No JWKS discovery, cache, or automated key rotation exists.
- No refresh-token, revocation, or session-freshness policy exists.
- No network API or OAuth/OIDC authorization flow exists.
- No database tenant-isolation guarantee exists.
- No provider dispatch or external retries are enabled.
- Only the supplemental identity dependencies are pinned here.
  A complete application dependency lock and audit are still required.

Tests use ephemeral local RSA signing keys held only in memory.
