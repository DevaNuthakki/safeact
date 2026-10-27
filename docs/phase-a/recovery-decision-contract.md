# Phase A — Recovery decision contract

## Status

Dependency-free domain prototype.

These decisions have unit tests. They have not been
integrated with durable storage, authorization, provider
adapters, or real external network dispatch.

## Operation identity

An operation binds:

- Operation identifier
- Tenant
- Business action type
- Target resource
- Provider account
- Connector
- Canonical payload fingerprint

The actual service must establish the operation identity
and prevent agents from inventing a replacement identifier
to bypass a business-action duplicate guard.

The payload fingerprint uses a restricted, deterministic
JSON encoding. It is not RFC 8785. The API must reject
duplicate raw JSON keys, enforce payload bounds, and hash
the exact validated outbound business payload.

Money should use integer minor units or explicitly
normalized decimal representations, never binary floats.

## Connector assumptions

Connector capability records are versioned.

Native retry protection is valid only when:

1. The operation identity remains unchanged.
2. The provider idempotency key remains unchanged.
3. The pinned connector contract still applies.
4. Concurrent same-key requests are protected.
5. The known lower-bound retention period remains valid,
   including a safety margin.
6. Retry authorization and budget permit another attempt.
7. The provider key scope is established by the adapter.

The test-only payments contract uses a simulated one-hour
retention with a five-minute safety margin.

> **Note (2026-10-26):** the sentence above records the Phase A contract. The current
> contracts are in `core/connector_profiles.py`:
> - payments (mock) and Stripe test mode: 24 h retention with a 1 h margin;
> - the qualification contracts in `chaos/contracts_qual_v1.json`: 24 h with a 2 s margin;
> - the qualification's short F09 variant: 6 s with a 2 s margin.
>
> The rule as it stands is summarized in the README section "When SafeAct retries".

It is not a statement about Stripe's real retention terms.

## Evidence trust boundary

Evidence objects are internal normalized inputs.

An evidence reference alone is not proof of application
or non-application. Only an authenticated and authorized
server-side verifier may classify authoritative evidence.

Before calling the policy, the service must establish:

- Source authenticity and collection provenance
- Correct operation and provider-account correlation
- Contract version and capability applicability
- Actor and tenant authorization
- Freshness and provider-specific interpretation

Agents, browser clients, human explanations, and
untrusted tool text cannot create authoritative evidence.

PROVEN_NOT_APPLIED requires proof that the original
request cannot still commit in the future.

A missing record, even from a strong current-state lookup,
does not automatically provide that guarantee.

## Decision table

| Observation | Policy decision |
| --- | --- |
| Changed operation identity, key, contract, or evidence binding | REJECT_CONFLICT |
| Verified effect applied | CONFIRM_APPLIED |
| Proven non-application, authorized retry budget | RETRY_AFTER_PROOF |
| Proven non-application without an authorized retry | CONFIRM_NOT_APPLIED |
| Inconclusive evidence with valid protected same-key retry | RETRY_SAME_KEY |
| Inconclusive evidence without safe retry and before deadline | VERIFY_LATER |
| Inconclusive evidence at or after deadline | ESCALATE_UNRESOLVED |

The investigation deadline is an escalation trigger.
It never transforms absence into non-application proof.

## Important implementation boundaries

A decision recommending retry is not network dispatch
authorization by itself.

Durable state transitions, unique constraints, outbox,
worker ownership, fencing, and provider checks must be
enforced before a future worker may dispatch.

The service must revalidate policy against current
durable state when claiming a retry.

Local fencing cannot stop an already-running remote call.

Compensation uses a distinct operation identity and
requires its own connector contract and verification.

## Qualification limits

Unit tests prove only the specified pure decision cases.

They do not yet prove crash safety, tenant isolation,
actual provider idempotency, or absence of duplicates
under all thread and process interleavings.

Further work will include integration, concurrency,
fault-injection, and independent-ledger verification.

## Phase A adversarial hardening

Decision timestamps must not predate the original
dispatch.

Native idempotency now uses a typed key scope and
explicit parameter-binding and concurrent-request
capabilities.

A contract reference is documentation metadata,
not independent proof of a real provider guarantee.

The payments fixture is simulated. Its retention
assumptions must not be attributed to Stripe.

### Trusted evidence boundary: still required

Before the application relies on decisive evidence,
a trusted server-side verifier must:

1. Authenticate and validate the evidence source.
2. Correlate evidence with the correct provider account.
3. Bind evidence to the immutable operation.
4. Cover all attempts that could still apply.
5. Persist provenance and verification timestamps.
6. Enforce authorization before network dispatch.
7. Revalidate durable state when executing a decision.

Caller-provided references, arbitrary flags, model
explanations, and browser assertions are not proof.

The current pure policy assumes its decisive evidence
was already established by this future trusted boundary.

The decision result must not be treated as a complete
dispatch authorization.
