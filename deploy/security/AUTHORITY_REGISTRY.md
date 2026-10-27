# SafeAct — Server-controlled actor registry

Status: Local, in-process policy snapshot implemented.
Production authoritative identity and grant storage pending.

## Identity binding

The registry maps an exact (issuer, subject) pair to one
internal (tenant_id, actor_id) pair and exact AdmissionGrant
records.

Issuer and subject refer to provider-issued identity.
Tenant and actor values originate from trusted server
configuration, not from JWT claims or client input.

The registry also checks the expected token audience.

## Fail-closed behavior

Unknown identities, disabled actors, absent grants,
duplicate identity bindings, duplicate actor mappings,
wildcards, invalid fields, and grants referring to
other tenants or actors are rejected.

Multiple identity providers mapping to the same internal
actor are intentionally unsupported in this version.
Federation requires a later explicit design.

## Trust assumptions

The configuration is trusted and provisioned by server
administrators. It is an illustrative local authority source,
not an external or persistent policy database.

Python dataclasses and instances of VerifiedPrincipal are
forgeable by code running inside the same process. The
registry's resolve_verified method MUST only be called
by trusted server code after real token verification.

Immutable configuration structures prevent accidental
changes; they do not provide process-level isolation.

## Important limitations

- No externally managed identity-to-actor database exists.
- Grants are static and do not have live revocation or expiry.
- Resource ownership is assumed in configured grants; it is
  not independently checked against a resource registry.
- No login, API server, or token transport is implemented.
- No automatic trusted JWKS refresh exists.
- PostgreSQL does not yet provide per-tenant row isolation.
- Direct database access can bypass the Python policy layer.
- No provider dispatch or automatic retries are enabled.

## Next integration gate

A single trusted entry point must accept only a raw token and
a proposed RequestedIntent. It must invoke the configured
IdentityVerifier, resolve the verified principal in this
registry, enforce exact-scope authorization, then submit
or observe the corresponding database intent.

Untrusted code must not be able to supply its own actor
context, grants, or preconstructed VerifiedPrincipal to
that entry point.

Later deployment work must replace the static registry
with authoritative, revocable, tenant- and resource-aware
policy resolution.
