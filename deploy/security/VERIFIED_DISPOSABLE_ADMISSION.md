# Verified JWT to disposable atomic admission

Status: Experimental adapter and integration tests only.

## Request path

1. Require a RequestedIntent with explicit scope identifiers.
2. Verify a raw RS256 JWT using pinned trusted public keys.
3. Take issuer and subject only from the verified token.
4. Canonicalize the payload with SafeAct's existing serializer.
5. Require an active caller-owned database transaction.
6. Check that the expected disposable runtime role is active.
7. Invoke the disposable SQL authorization/admission function.
8. Return a SubmissionReceipt without committing.

The function checks persistent identity, actor, resource,
and exact grant records, holding row locks through the
caller-owned transaction.

## Test coverage

Integration tests use signed JWTs and disposable PostgreSQL
schemas. They cover forged claims, invalid signatures,
expired tokens, unknown and disabled identities, disabled
actors and grants, scope denial, canonical serialization,
idempotency, cross-actor protection, rollback, and concurrent
revocation.

## Mandatory security limitations

This is not production authentication or credential isolation.

The SQL function still accepts issuer/subject strings as
arguments. A caller with its EXECUTE credential can invoke
the function directly and assert another enabled identity.
The existing negative-control tests demonstrate this.

The local development database login is a superuser.
Tests use SET LOCAL ROLE to simulate restricted execution;
this does not prove separation of real deployment credentials.

A trusted Python wrapper does not prevent a compromised
application process or stolen database credential from
invoking SQL directly.

The SQL function still does not independently require
canonical JSON, even though this adapter supplies it.

Transaction commit failures can be ambiguous. The adapter
never automatically retries a failed or uncertain commit.
The caller must reconcile the original request key safely.

No external identity provider, HTTP ingress, separate
production credential, production migration, permission
administration API, or external provider dispatch is enabled.

Before deployment, design and test real credential isolation,
trusted identity provenance, and database function invocation
rights. The prototype must not be deployed as-is.


## Explicit integration-test opt-in

The signed-token PostgreSQL integration test class is skipped
unless all three environment variables are set to `1`:

- `SAFEACT_RUN_DB_INTEGRATION`
- `SAFEACT_RUN_DISPOSABLE_DB_TESTS`
- `SAFEACT_RUN_DB_SECURITY_TESTS`

Default test discovery must not create database schemas or roles.

A separate regression test also rejects an active database
transaction running under the unexpected execution role.

This role check is a configuration safeguard only. It does
not establish secure credential isolation or prevent a caller
holding privileged database credentials from bypassing it.
