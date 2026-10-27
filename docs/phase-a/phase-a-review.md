# Phase A — Engineering review

## Status

Contract-spike implementation under review.

The work is not a production-ready dispatcher.

## Implemented

- Immutable in-memory operation identity
- Restricted canonical payload fingerprint
- Versioned simulated connector contracts
- Explicit idempotency capability requirements
- Conservative recovery decision engine
- Bounded retry decision rules
- Deterministic late-commit simulation
- Independent simulated effects ledger
- Adversarial contract tests
- Guarded recovery experiment

## Critical safety behavior

An eventually consistent NOT_FOUND lookup is not
definitive proof of remote non-application.

Without a valid provider retry guarantee, the
policy must preserve uncertainty or escalate.

The original remote request may commit later.

## Known limitations

1. Operation identity is not persisted.
2. Tenant and principal authorization are not implemented.
3. Evidence authenticity is not independently verified.
4. Outstanding-attempt coverage is not implemented.
5. No database-backed transition or outbox exists.
6. No concurrent workers or distributed locks exist.
7. Native idempotency assumptions remain mock contracts.
8. Provider retention has not been verified against
   a real provider sandbox.
9. The policy is not wired to a live dispatcher.
10. No production safety or reliability claim is made.

## Architectural gate before real dispatch

A network dispatcher must not consume arbitrary
caller-provided authoritative evidence.

A trusted server-side verification boundary must
establish provenance, operation binding, provider
guarantees, and coverage of relevant attempts.

Retry decisions must also be checked against current
durable state, authorization, and worker ownership.

## Qualification status

Unit tests and deterministic simulation only.

No independent external review has been completed.

No real provider, concurrency, worker crash, database
failure, load test, or security-boundary qualification
has been performed.

The project cannot yet claim production safety.

## Next architecture milestone

Durable operation identity, audit transitions,
outbox persistence, trusted evidence provenance,
and safe worker ownership.
