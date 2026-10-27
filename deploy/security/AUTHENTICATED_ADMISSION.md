# SafeAct — Authenticated admission boundary

Status: Framework-independent service and tests implemented.
No external HTTP or identity-provider integration exists.

## Trusted request path

1. Accept a raw signed token and RequestedIntent.
2. Verify the token using a trusted offline RS256 verifier.
3. Resolve its issuer/subject against server-controlled policy.
4. Pass the resolved actor and grants to exact-scope admission.
5. Submit or observe through the existing PostgreSQL adapter.

The public service methods do not accept actor or grant
arguments from the caller.

## Failed requests

An invalid signature, expired token, unknown identity,
disabled actor, missing grant, or unauthorized scope must
fail before the service accesses PostgreSQL.

The unit tests use a database poison object and the
integration tests use isolated disposable PostgreSQL schemas.

## Existing operation ownership

The original INTENT_RECORDED audit actor must match a
later observer or idempotent re-submitter of the same
tenant-scoped request key.

Cross-actor operation claims must remain denied.

## Limits and assumptions

This is an in-process service, not an enforced network
authentication boundary. Existing lower-level Python
storage functions remain callable by trusted process code.

A compromised process or untrusted code running in that
same process could bypass the service. The deployment must
restrict external ingress to an authenticated entry point.

The current authority registry is a static trusted
configuration snapshot. It does not provide live grant
revocation, independently verified resource ownership,
or durable identity-to-actor account management.

The identity verifier does not automatically refresh
issuer public keys and has no session revocation mechanism.

The local development database superuser remains unsuitable
as a production application credential.

No HTTP server, OAuth login flow, row-level isolation,
external provider dispatch, or automatic unsafe retry
is implemented at this milestone.
