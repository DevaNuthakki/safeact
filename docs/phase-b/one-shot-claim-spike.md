# SafeAct — One-Shot PostgreSQL Claim Experiment

Status: experimental, not a production execution design.
Scope: Phase B, durable execution admission research.

## Question being tested

Can PostgreSQL guarantee that, for one committed SafeAct
operation, multiple concurrent contenders cannot independently
reserve the same initial execution opportunity?

The experiment is restricted to an isolated per-test schema.

## Candidate reservation primitive

A separate append-only reservation table has:

- An immutable UUID reservation identity.
- A foreign key to a committed SafeAct operation.
- A worker label for diagnostic purposes only.
- A UNIQUE constraint on operation_id.
- A CHECK constraint requiring dispatch_enabled = false.
- A trigger rejecting UPDATE and DELETE.

Concurrent INSERT ... ON CONFLICT DO NOTHING RETURNING
statements race against the unique constraint.

At most one statement can commit a reservation for an operation.

The returned reservation is NOT a dispatch capability,
authorization credential, or permission to perform an effect.

## Deliberate restrictions

There is no lease renewal, expiry-based reclaim, or takeover.

A lost worker can block progress indefinitely. This is
intentional for the experiment: safety is investigated before
liveness or recovery.

The worker label is not an authenticated principal and must
not become an execution authorization source.

No actual provider adapter is reachable.

No production migration or persistent reservation table
is created. Test cleanup drops its isolated schema.

## Existing protections preserved

Admission operations, initial audit events and outbox events
are not modified by the reservation experiment.

The initial outbox explicitly disables dispatch.

Original SafeAct tables retain their write-guard triggers.

The admission database role receives no new privileges.

The existing recovery inspection remains negative-only.

## Claims this experiment may support

It may demonstrate one-shot reservation uniqueness under
concurrent PostgreSQL transactions.

It may demonstrate rollback behavior, FK binding,
insert-only reservation history and forced dispatch denial.

It does not establish authenticated worker identity,
tenant isolation, production role permissions, durable
leases, attempt-risk transition correctness, provider
idempotency, protection from stale workers, or actual
dispatch enforcement.

An expired reservation would not prove that a provider
effect was not applied.

## Required next design gate

Before building a provider worker:

1. Define separately authenticated service authority.
2. Define an append-only claim and attempt event protocol.
3. Establish atomic ownership and retry authorization.
4. Persist MAY_HAVE_DISPATCHED before crossing the
   external-effect boundary.
5. Prevent a stale claimant from acting after losing
   authorization.
6. Separate provider evidence provenance from request input.
7. Prove independent provider-attempt counts and fault recovery.
8. Provide database least-privilege enforcement and tenant
   ownership checks.

No provider dispatch will be enabled until these boundaries
are implemented and tested.

SA-EVAL-003 remains NOT_RUN. This experiment is not its
provider retry-enforcement oracle.
