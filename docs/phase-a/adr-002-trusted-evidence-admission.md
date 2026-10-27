# ADR-002: Trusted Evidence Admission and Durable Decisions

Status: PROPOSED
Scope: SafeAct Phase A to Phase B boundary
Implementation status: NOT IMPLEMENTED

## 1. Problem

The current RecoveryRequest accepts normalized Evidence
and an AttemptInventory.

These are data contracts, not security boundaries.

An untrusted caller could construct objects claiming that
an operation was not applied and that the inventory is
complete.

The deployed service must not trust those claims.

## 2. Trust boundaries

Untrusted inputs include:

- Browser requests
- MCP clients and agent tool arguments
- Webhook payloads before authentication
- Human-entered references
- Provider response text before adapter validation

Trusted authority is established by server-side controls,
not Python object types or caller-supplied boolean flags.

## 3. Evidence admission

A trusted provider adapter collects observations using
its configured provider identity and credentials.

Evidence admission must validate:

- Provider origin and authentication where applicable
- Operation and provider-account correlation
- Connector contract and version
- Attempt or effect correlation
- Observation timestamp and freshness
- Provider-specific status interpretation
- Whether the result is authoritative or inconclusive

Webhook signatures, when applicable, must be checked
against the original signed payload.

Untrusted provider text is never treated as instructions.

An evidence reference is metadata, not proof by itself.

## 4. Durable attempt inventory

The database is authoritative for recorded attempts.

The application must derive the inventory from durable
records, rather than accepting an inventory from a caller.

Attempt completeness requires accounting for every
possibly dispatched attempt belonging to the operation.

A missing attempt or ambiguous worker-loss boundary must
cause conservative investigation, not assumed rejection.

## 5. Atomic authorization boundary

Before authorizing a dispatch or retry:

1. Authenticate the actor.
2. Enforce tenant, action, and resource permissions.
3. Lock or otherwise protect the current operation state.
4. Load the authoritative attempt inventory.
5. Validate admitted evidence and connector guarantees.
6. Evaluate the recovery decision against current state.
7. Enforce expected operation and ownership versions.
8. Persist the decision, audit event, and next work item
   in one transaction.
9. Dispatch only through the authorized durable work path.

Do not authorize a remote request from an old inventory
snapshot after another worker has created a new attempt.

## 6. Required database properties

The Phase B design must address:

- Unique operation identity within its permitted scope
- Immutable operation payload fingerprint
- Attempt identity and operation foreign key
- Explicit dispatch-risk tracking
- Append-only evidence and audit history
- Transactional outbox records
- Version-checked state transitions
- Tenant-scoped authorization
- Consistent attempt-inventory reads
- Idempotent processing of duplicate queue deliveries

CHECK constraints alone cannot enforce prior-state
transition legality.

State transitions require transactional enforcement.

## 7. Worker and provider boundaries

Local leases and ownership versions fence database writes.

They do not fence an external provider request already
in progress.

Worker crashes, expired leases, Redis loss, and duplicate
queue delivery must preserve uncertainty.

A second network call requires separate provider-backed
retry eligibility, not merely a new local worker lease.

## 8. Adversarial acceptance tests

E01: Caller submits fabricated decisive evidence.
E02: Caller supplies is_complete=True.
E03: Evidence belongs to another provider account.
E04: Evidence belongs to another operation.
E05: A second attempt appears after a stale snapshot.
E06: Provider lookup returns NOT_FOUND before late commit.
E07: Expired worker ownership attempts a stale write.
E08: Duplicate outbox delivery creates competing workers.
E09: Provider webhook is replayed or out of order.
E10: Database commit acknowledgement is lost.
E11: Provider evidence arrives after escalation.
E12: Conflicting definitive observations are received.

Safety results must be checked against an independent
effects ledger where a controlled provider is available.

## 9. Implementation decisions

Use PostgreSQL as the durable authority.

Keep Redis and Celery out of the transaction correctness
boundary; broker messages may be duplicated or lost.

Implement trusted evidence ingestion at the server-side
adapter boundary, backed by durable provenance records.

The pure recovery engine remains advisory until its
inputs are produced by trusted application services.

## 10. Exit requirements

Before real dispatch is enabled:

- Durable operation and attempt records exist.
- Evidence admission rejects untrusted proof claims.
- Tenant and principal authorization is enforced.
- Decisions use consistent inventory and current versions.
- Retry authorization survives concurrency and failures.
- Audit and outbox behavior is transactionally verified.
- Fault tests demonstrate prohibited retries are blocked.

This ADR does not establish that these requirements have
been implemented or independently qualified.
