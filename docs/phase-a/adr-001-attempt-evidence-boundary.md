# ADR-001: Attempt Inventory and Trusted Evidence Boundary

Status: PROPOSED
Scope: SafeAct Phase A
Implementation: NOT YET COMPLETE

## 1. Context

One logical operation may have several dispatch attempts.

A timeout, worker crash, lost acknowledgement, or stale
provider lookup cannot establish that an earlier attempt
will never produce an effect.

An authoritative observation about one attempt does not
automatically resolve every attempt for that operation.

## 2. Core entities

### Operation

An immutable logical business action with stable:

- Operation ID
- Tenant and authorized action
- Resource and provider account
- Canonical payload fingerprint
- Connector and pinned capability contract
- Provider idempotency identity when supported

### Attempt

Each network attempt has a distinct immutable attempt ID.

An attempt references exactly one operation.

Its record must preserve:

- Local attempt ID and operation ID
- Provider idempotency key, if applicable
- Dispatch risk and ownership information
- Provider request or correlation reference, if known
- Dispatch and observation timestamps
- Evidence and audit references

A worker losing ownership must not erase an attempt or
imply that remote execution stopped.

### Evidence

Evidence must record:

- Trusted collection source and provenance
- Provider account and operation correlation
- Relevant attempt or provider effect references
- Connector capability-contract version
- Collection and provider-observation timestamps
- Normalized observation and interpretation
- Raw evidence reference or integrity fingerprint

A reference string alone does not establish authenticity.

## 3. Dispatch risk

Distinguish:

A. Definitively not dispatched, supported by local
   durable execution evidence.

B. Possibly dispatched or remotely pending.

C. Confirmed applied, supported by provider evidence.

D. Definitively unable to apply, supported by a provider
   guarantee and authoritative terminal evidence.

State B cannot become state D merely because a lookup
returns NOT_FOUND or a local lease expires.

## 4. Negative evidence coverage

Before treating an operation as definitively not applied:

1. Obtain the complete durable attempt inventory.
2. Include every attempt that could have reached the
   remote provider.
3. Establish an authoritative terminal non-application
   guarantee for every such attempt.
4. Confirm that no attempt may still commit later.
5. Bind evidence to the correct operation, account,
   provider contract, and relevant attempt identifiers.
6. Reject missing, conflicting, or insufficient evidence.

An empty lookup does not satisfy this contract.

Proof that attempt A was rejected is insufficient when
attempt B may still be running.

## 5. Native idempotency exception

An ambiguous action may be retried using the same
provider key only within independently established
native provider guarantees and the authorized budget.

This is different from proving non-application.

The implementation must respect:

- Key namespace and account scope
- Payload and parameter binding
- Concurrent-request behavior
- Guaranteed retention and safety margin
- Provider-specific errors and rate limits

Local fencing does not establish provider-side fencing.

## 6. Trusted evidence admission

Only a trusted server-side provider adapter may submit
authoritative evidence to the decision boundary.

The future service must verify its provenance and scope.

Browser, agent, human-entered text, and arbitrary tool
results may request investigation but must not upgrade
an uncertain result to authoritative proof.

Python types and boolean flags alone are not sufficient
security or authenticity boundaries.

## 7. Durable decision boundary

Future implementation must evaluate evidence against a
consistent durable operation and attempt inventory.

The transaction that records an authorized state change
must enforce:

- Legal transition
- Expected version and ownership
- Current authorization and connector contract
- Audit event and durable next action
- Conflict rejection if relevant state has changed

No network dispatch may occur before durable intent.

Decisions based on stale inventory must be recomputed.

## 8. Conflicting and late evidence

A late worker response may be preserved as evidence even
when the worker has lost local ownership.

Conflicting provider observations require investigation.

No newer state may silently erase a prior applied effect.

A compensation is a separate action with its own attempt
history, evidence, and verification.

## 9. Required negative tests

N01: A rejected, B still remotely pending.
N02: A lookup NOT_FOUND, A commits later.
N03: Provider effect confirmed, older rejection arrives.
N04: Missing attempt from evidence coverage.
N05: Evidence correlated to another provider account.
N06: Untrusted caller presents fabricated proof.
N07: Native idempotency protection expires.
N08: Late worker updates after losing local ownership.
N09: Concurrent reviewer or worker uses stale version.
N10: Duplicate provider notification is delivered.
N11: Database commit acknowledgement is lost.
N12: Compensation remains uncertain.

## 10. Acceptance boundary

This ADR specifies intended architecture, not observed
correctness.

Phase A must implement and test the pure attempt-coverage
decision rules before its contract gate is complete.

Durable enforcement and real evidence admission must be
qualified during later implementation phases.

A pure function must not be exposed as permission to
dispatch external side effects without those controls.
