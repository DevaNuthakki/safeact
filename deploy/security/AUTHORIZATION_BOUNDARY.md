# SafeAct admission authorization boundary

Status: Pure policy contract verified; authentication integration pending.

## Decision

Keep the durable storage service separate from authentication.

The policy module `core/admission_authorization.py` accepts:

1. TrustedActorContext — verified tenant and actor supplied by
   trusted server-side authentication code.
2. AdmissionGrant tuples — exact permissions sourced from a trusted
   server-side policy registry or authorization decision service.
3. RequestedIntent — proposed action and payload from a caller.
   This structure intentionally has no tenant_id or actor_id.

The output, AuthorizedIntent, binds the server-supplied identity
to a canonical UTF-8 snapshot of the proposed payload.

The policy requires exact matching across tenant, actor, connector,
provider account, action, resource, and contract version.
It supports no wildcard permissions or implicit fallback.

## What this module does NOT do

- It does not authenticate a network client or validate a token.
- Python dataclass instances are not unforgeable security credentials.
- It does not establish that grants originated in a trusted registry.
- It does not prove that a resource belongs to a tenant.
- It does not implement revocation, expiry, or session freshness.
- It does not prevent code with raw database access from bypassing it.
- It does not prevent a compromised internal service from accessing
  another tenant's records through its database grants.
- It does not authorize provider dispatch or external retries.

## Internal authorization-to-storage adapter

`storage/authorized_submission.py` now defines internal
`submit_scoped_intent` and `observe_scoped_intent` entry points.

Both evaluate the exact-scope policy before calling the
existing database submission or observation functions.

Both reconstruct the storage request from the canonical
authorized payload snapshot, not from a mutable proposal.

This is NOT a remotely authenticated security boundary.
The calling server must still establish the authenticity
and freshness of context and grants.

Direct use of `storage/submission.py` remains possible for
trusted internal code. Python module boundaries do not
prevent malicious code from accessing the database.

Actor ownership of tenant-scoped idempotency keys remains
an unresolved security contract and must be addressed
before externally exposing these entry points.

## Required later integration

The future trusted admission entry point must:

- Authenticate the actor through a verified identity mechanism.
- Resolve allowed tenant membership and exact grants server-side.
- Validate the action, resource ownership, provider-account access,
  connector binding, and contract version against authoritative data.
- Never accept caller-provided tenant/actor identities or permissions.
- Bind an AuthorizedIntent into the validated IntentSubmission.
- Call the transactional storage service only after authorization.
- Deny rejected requests before any database admission write.
- Protect read-only observation APIs with the same scope.
- Define idempotency-key visibility/ownership rules, including
  cross-actor replay within the same tenant.

## Test claims

The Step 31B pure unit tests cover exact-match authorization,
negative scope cases, invalid context/grant shapes, forbidden
wildcard scopes, malformed requests, and payload snapshot stability.

These tests verify policy behavior when trusted inputs are supplied.
They are not evidence of an implemented identity provider or
production tenant isolation.

PostgreSQL RLS and a network API remain separate architecture
decisions requiring an explicit threat-model and identity design.
