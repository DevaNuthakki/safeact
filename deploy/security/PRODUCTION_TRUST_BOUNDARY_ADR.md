# SafeAct — Production Admission Trust-Boundary Decision

**Status:** Proposed architecture; implementation and production approval pending.

**Baseline:** Git checkpoint `75b2f8e`, Step 38A audit.

**Database migration:** `0002_canonical_payload`.

**Provider dispatch:** Not authorized by this decision.

This document is an engineering design contract, not a claim that
SafeAct currently operates with production-grade isolation.

## 1. Verified starting evidence

The existing identity verifier validates signed RS256 JWTs against
pinned public JWKs, configured issuer, audience, and lifetime rules.

The existing authenticated admission service resolves identities
through an in-memory authority registry before calling the
database submission path.

The disposable SQL prototype independently resolves persistent
identity, actor, resource, and exact grants while holding database
authorization locks through the admission transaction.

The disposable HMAC proof protocol binds fifteen request fields,
including verified issuer and subject, canonical JSON, payload
digest, proof key identifier, timestamps, and a nonce.

The experimental PostgreSQL implementation has passed tests
covering direct-SQL impersonation denial, tampering, expiry,
replay, transaction rollback, and concurrent revocation.

The current development role `safeact_dev` is a superuser.
The legacy non-login `safeact_admission_writer` role retains
direct INSERT permissions on the public operation, audit, and
outbox tables.

No permanent authority tables or proof-admission functions
have been deployed.

## 2. Security objective

A caller possessing only the application database-admission
credential must not be able to impersonate another verified
identity, forge a different request, bypass current grants,
or write an admission bundle directly.

A caller possessing only public API access must not obtain an
admission proof without a valid identity token and authorization
to invoke the internal signing workflow.

The system must fail closed on uncertain identity, authority,
proof, database, and commit outcomes.

## 3. Threat-model boundaries

### In scope

- Unauthenticated and malicious external API callers
- Forged or expired JWTs
- Forged actor, tenant, or permission claims
- Compromised admission-only PostgreSQL credentials
- Unauthorized SQL function execution
- Altered and replayed admission proofs
- Concurrent grant revocation
- Transaction failure and ambiguous commits
- Accidental application-role privilege escalation
- Misconfiguration during migration or function provisioning

### Not automatically solved

- Compromise of the trusted proof signer
- Theft of the HMAC signing secret
- Compromise of PostgreSQL superuser or migration authority
- Compromise of an identity provider's signing keys
- Complete compromise of the application and its active user tokens
- SQL or infrastructure vulnerabilities outside the tested contracts

These remain important threats requiring operational controls.

## 4. Proposed minimal process architecture

### Admission API

Responsibilities:

1. Accept the authenticated request and raw JWT.
2. Validate request shape and transport-level constraints.
3. Authenticate to an internal proof-signing endpoint.
4. Forward the raw JWT and exact requested operation.
5. Receive the request-bound proof.
6. Open a transaction using its restricted PostgreSQL credential.
7. Submit the proof to the protected admission gate.
8. Commit once and return the definitive receipt only when known.

The API must not contain the HMAC signing secret.
The API database role must have no authorization-table or
application-table write permissions.

### Isolated proof signer

Responsibilities:

1. Accept requests only from authenticated, authorized workloads.
2. Verify the raw signed JWT independently.
3. Ignore caller-supplied actor, tenant, or grant assertions.
4. Reuse SafeAct's canonical JSON serializer.
5. Bind the exact request and verified identity to a proof.
6. Apply bounded timestamps, nonce generation, and key selection.
7. Return proof material without retaining a database connection.

The signer must not expose an unrestricted
"sign arbitrary issuer and subject" interface.

Compromising the API's database credential must not provide
access to the signer key or authorization to mint arbitrary proofs.

The precise service-authentication mechanism is an open decision
requiring a deployed, adversarially tested implementation.

### PostgreSQL proof gate

Responsibilities:

1. Accept only the complete, typed proof contract.
2. Verify proof key status, MAC, time bounds, and nonce.
3. Check persistent identity, actor, resource, and exact grants.
4. Lock authorization records against concurrent revocation.
5. Atomically record operation, audit evidence, and
   no-dispatch outbox event.
6. Preserve idempotency and deny conflicting original-actor evidence.
7. Never invoke external providers.

The proof verifier and authorization checks must execute within
the same caller-owned database transaction.

## 5. Database roles and privileges

Target roles are logical design names; they have not been created.

| Role | Login | Intended privileges |
| --- | --- | --- |
| Migration operator | Controlled only | Schema DDL and audited provisioning |
| Protected function owner | No | Required table and verification-key access |
| Admission execution role | No | EXECUTE protected gate only |
| Admission API login | Yes | Membership permitting protected gate invocation only |
| Proof signer | No database login | Access to signing key through isolated secret delivery |

Mandatory properties:

- Runtime credentials cannot SET ROLE to a table writer,
  migration operator, or SECURITY DEFINER function owner.
- Runtime credentials cannot directly INSERT, UPDATE, DELETE,
  or SELECT protected authority, audit, outbox, or operation data.
- Runtime credentials cannot execute the old unprotected gate.
- Legacy admission-writer grants and role memberships must be
  eliminated from every production runtime path.
- Privileges obtained through PUBLIC, role inheritance,
  or nested membership must be explicitly audited.
- Function-owner privileges must be minimized.
- Functions use fixed, audited search paths and schema-qualified
  references.

Migration provisioning must be atomic. PostgreSQL functions
normally receive PUBLIC EXECUTE permission on creation.
Production function creation, ownership, revocation, and
restricted grants must not expose a callable privileged function
to untrusted roles during an online migration.

No authorization migration may rely on the development
superuser credential being present in the application runtime.

## 6. Proof and canonicalization contract

The experimental proof format contains:

- Explicit protocol domain
- Verified issuer and subject
- Request and idempotency keys
- Exact action, resource, connector, account, and contract version
- Canonical JSON text and SHA-256 digest
- Proof verification-key identifier
- Bounded issuance and expiry timestamps
- High-entropy nonce

All signed fields must retain an unambiguous,
versioned binary or text encoding.

The signer must enforce the canonical serializer's restricted
JSON contract. The database currently verifies signed bytes
but does not independently prove that the text follows all
Python canonicalization rules.

Production acceptance requires a precise decision about
which trusted boundary enforces canonicalization and how
that contract is independently verified.

A change to serializer or proof encoding must introduce an
explicit compatibility/versioning decision.

## 7. Key custody and rotation

The prototype uses a shared symmetric HMAC key.

The signing component and PostgreSQL verification owner
both require access to that secret in the experimental design.

The production design must explicitly specify:

- Secure signing-key provisioning and storage
- Separation from application database credentials
- Verification-key access controls
- Key identifiers and activation status
- Rotation overlap and maximum proof lifetime
- Immediate revocation of compromised keys
- Recovery from suspected signer or key compromise
- Audit records for key administration
- Removal of expired keys and associated nonce records

No keys, tokens, passwords, or production secrets may be
committed to source control or displayed in test output.

The SQL bytea equality comparison is not established as
constant-time. A reviewed MAC-verification mechanism is a
deployment requirement; passing functional tests alone does
not satisfy it.

## 8. Replay and commit ambiguity

A committed nonce must not be reusable.

Nonce insertion and the admission bundle must commit
in the same database transaction.

A rolled-back transaction also rolls back nonce consumption.
A still-valid proof may therefore be retried after a confirmed
rollback. This does not authorize blind retry after an
uncertain commit.

SafeAct's existing uncertain-submission and observation
contracts must remain authoritative:

- A confirmed matching committed bundle permits safe
  idempotent reconciliation.
- Conflicting identity, original actor, payload, or audit
  evidence must deny or hold the operation.
- Missing rows alone do not prove a late commit impossible.
- Unknown commit outcomes remain UNKNOWN/HOLD until sufficient
  evidence establishes the safe next action.
- External provider effects remain disabled during admission.

Nonce cleanup must account for proof expiry, outstanding
transactions, and any approved reconciliation horizon.

## 9. Architecture alternatives

### A. Single trusted API and restricted database gate

This is simpler to deploy and maintain.

However, if the gate accepts untrusted issuer/subject
arguments, a stolen gate credential can assert another
enabled subject. Step 37A demonstrated this weakness.

Not selected for the stated database-credential compromise goal.

### B. Isolated proof signer and proof-only database gate

This retains the verified request-bound proof contract and
separates the signing secret from the admission API credential.

This is the selected candidate for the next security prototype,
subject to key-custody, service-authentication, MAC-verification,
and deployment-privilege acceptance gates.

It is not yet approved for production.

### C. Database-side JWT verification

This could avoid sharing a symmetric HMAC key with the signer.

However, a safe implementation would need independently reviewed
signature verification, JWK management, issuer/audience/time
validation, and claim parsing within the database boundary.

It is not selected without evidence that it offers lower
implementation and operational risk.

## 10. Required acceptance gates

### Gate A — Signer isolation

A separate signer process authenticates its callers, verifies
JWTs, and issues proofs. The admission API cannot access its
secret or mint arbitrary proofs.

Test from separate processes, not only within one Python interpreter.

### Gate B — Credential isolation

An independently authenticated PostgreSQL runtime login can
execute only the protected gate.

It cannot access verification secrets, legacy writer
capabilities, tables, function ownership, or unrestricted SQL gates.

### Gate C — Cryptographic and canonical contract

Valid proofs pass; every signed-field mutation, invalid JWT,
expired proof, revoked key, invalid MAC, and replay attempt
fails in the expected layer.

Timing behavior and canonicalization assumptions receive
independent review.

### Gate D — Transaction and concurrency safety

Operation, audit, no-dispatch outbox, authorization locks,
and nonce consumption share the same transaction.

Concurrency, rollback, conflicting replay, grant revocation,
and ambiguous commit outcomes are tested.

### Gate E — Safe migration rehearsal

Rehearse the complete DDL and privilege lifecycle in an
isolated database.

Verify atomic PUBLIC revocation, schema permissions,
role membership, legacy-writer removal, and failure cleanup.

A failed migration must not leave an unsafe admission path.

### Gate F — Operational and security review

Document and test key rotation, secret delivery, audit logging,
network isolation, credential revocation, and recovery.

No production deployment or provider dispatch before approval.

## 11. Implementation sequence

1. Freeze this design and resolve open security choices.
2. Build a disposable real-process signer boundary.
3. Add independent signer/API authentication and negative tests.
4. Verify real PostgreSQL credential and role separation.
5. Resolve MAC comparison and key-rotation requirements.
6. Rehearse atomic migration and role revocation.
7. Verify commit reconciliation, failure injection, and holdouts.
8. Review the final contract before any permanent migration.

Initial experiments can use existing Python, PostgreSQL, and
Docker Compose facilities. A cluster orchestrator, task queue,
cache, or additional infrastructure is not required merely to
demonstrate this boundary.

## 12. Open decisions

- Internal service-authentication method
- Production key storage and delivery mechanism
- MAC-verification timing mitigation or alternative
- Whether to retain symmetric proofs or adopt an independently
  verified asymmetric construction
- Exact privilege migration and legacy-writer retirement plan
- Canonical payload enforcement and version compatibility
- Key rotation and nonce retention policies
- Reconciliation evidence for uncertain commits
- Deployment and operational threat-model approval

**Decision status: Architecture candidate only.**

No permanent database migration or external provider dispatch
is authorized by this document.
