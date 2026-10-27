# SafeAct — Disposable HMAC admission proof

Status: Experimental security prototype, not deployable.

## Proven protocol properties under test

- A trusted test signer accepts a raw JWT and verifies RS256
  against pinned public keys.
- Issuer and subject are taken only from verified claims.
- The existing SafeAct serializer derives canonical JSON
  and the payload SHA-256 digest.
- HMAC-SHA256 binds the exact request, identity, proof key ID,
  issuance, expiry, and random nonce.
- A PostgreSQL SECURITY DEFINER wrapper checks the MAC,
  proof freshness, and nonce before calling the existing
  atomic admission function.
- The runtime role cannot read the HMAC key or protected tables.
- The runtime role cannot invoke the original unprotected gate.
- Proof nonce insertion is part of the admission transaction.
- Authority row locks and operation/audit/outbox commit remain
  in the same transaction.
- Invalid proofs, expired proofs, and revoked permissions deny.
- A fresh proof preserves same-actor idempotency.

## One-time proof semantics

A committed proof nonce cannot be reused.

A transaction rollback also rolls back its nonce insertion.
Consequently the same signed proof can be submitted again while
valid after rollback. This is intentional transactional behavior,
not durable rejection of all previously attempted proofs.

Commit ambiguity must not lead to blind retry.

## Unresolved production security requirements

This uses symmetric HMAC with a key held by the test signer
and by PostgreSQL. Both environments must be secured.

The development database is a superuser environment, although
a separate non-superuser TCP credential is tested.

The SQL equality comparison is not established as constant-time.
Its timing characteristics require independent review.

Canonical JSON is supplied by the trusted signer. The older
unprotected SQL function remains deliberately available to
its dedicated owner inside the disposable schema only.

Proof signature generation must be isolated from untrusted
application/database callers. Key rotation, key compromise,
secret distribution, auditing, nonce retention and pruning,
time synchronization, session authentication, and network
exposure remain unresolved.

No permanent migration, provider dispatch, production API,
deployed signer, or production credential changes are included.

Do not apply this experimental SQL helper to a permanent
database schema.

## Additional deployment-blocking audit findings

**Function provisioning must be atomic.** PostgreSQL grants
EXECUTE on newly created functions to PUBLIC by default.
The disposable test fixture creates its functions and then
changes ownership and privileges in subsequent statements.
This is suitable only for the isolated experiment, not for a
permanent online migration. Production DDL must ensure that
untrusted roles cannot execute a new SECURITY DEFINER function
during provisioning, including through inherited PUBLIC grants.

**Function-owner privileges require review.** The experimental
function owner has access to protected authority records and
the verifier key table. The production role design must restrict
ownership, membership, table privileges, search paths, and
function execution without privilege-escalation paths.

**Legacy writer bypass remains unresolved.** The existing
safeact_admission_writer role can directly insert application
records in the current development schema. It cannot be reused
as the production proof-only runtime role.

**The signature boundary is still a trust assumption.**
The HMAC signer must independently verify JWTs and cannot
expose its signing key or arbitrary-signing functionality to
callers holding only database-admission credentials.

**Timing behavior is not established.** PostgreSQL bytea
equality used for MAC comparison has not been verified as
constant-time. The deployed verifier requires a separately
reviewed comparison mechanism or a different protocol.

**Proof lifecycle remains incomplete.** Production work
must define key rotation, revoked keys, nonce pruning,
commit-ambiguity reconciliation, signer compromise recovery,
and canonical-payload enforcement.

These findings prohibit production deployment of this
disposable experiment even when every regression test passes.
