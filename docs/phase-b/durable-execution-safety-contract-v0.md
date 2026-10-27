# SafeAct — Durable Execution Safety Contract v0

Status: PROPOSED FOR REVIEW.
Phase: B — durable execution.
Classification: design contract, not implemented worker behavior.

This document defines the minimum safety boundary needed
before any fake-provider execution experiment.

It does not authorize provider dispatch, production
migration, deployment, or changes to evaluation verdicts.

## 1. Verified baseline

SafeAct currently provides:

- Immutable, durable admission bundles in PostgreSQL.
- Authenticated admission and observation.
- Negative-only authenticated recovery inspection.
- A pure recovery decision policy.
- Append-only initial attempt schema constraints.
- A tested PostgreSQL one-shot claim experiment.

The one-shot claim table is test-only, not a production
worker-ownership mechanism.

Existing admission outbox records have dispatch disabled.

No production execution worker or trusted dispatch
authorization interface has been established.

## 2. Fundamental safety invariant

A durable operation is not evidence of a provider effect.

A successful claim is not permission to call a provider.

A provider effect must never be initiated from an UNKNOWN
admission, an untrusted caller flag, or a diagnostic HOLD
result.

No worker may infer non-application from:

- A timeout.
- A lost acknowledgment.
- A missing provider lookup result alone.
- An expired or missing lease.
- A crashed worker.
- A missing local completion event.
- A provider response that lacks trusted provenance.

## 3. Trust boundaries

The admission caller is not the worker authority.

The authenticated actor identity is resolved by trusted
server configuration, never from a caller-created dataclass.

A future execution service must separately authenticate
its own service principal.

The execution service must bind authority to:

- Immutable operation ID.
- Tenant and original actor ownership.
- Connector and provider-account identity.
- Action and resource.
- Canonical payload digest.
- Pinned connector contract version.
- Native provider idempotency key, when applicable.
- Durable attempt and claim identities.
- Trusted retry-policy decision.

Possession of an operation UUID, reservation UUID,
database row, or serialized Python object does not
confer dispatch authority.

Worker-label strings are diagnostic only.

## 4. One-shot reservation protocol

Initial execution research will use one reservation
per operation, enforced by a PostgreSQL UNIQUE constraint.

Reservation insertion must be atomic.

A losing contender must not execute any external effect.

A rolled-back reservation is not committed ownership.

A committed reservation cannot be reassigned, edited,
deleted, or automatically reclaimed in this version.

The absence of takeover is intentional.

A crashed claimant may block progress indefinitely.

Safety is prioritized over automatic recovery.

## 5. Durable dispatch-risk fence

A separate append-only event is proposed:

DISPATCH_RISK_COMMITTED

Its meaning:

The system has durably acknowledged that the associated
attempt may cross the external-effect boundary.

This event is not proof that a provider call occurred.

It is not proof that the provider applied an effect.

The event must be bound to an authenticated and
authorized reservation and attempt.

The database must enforce uniqueness and identity
binding independently of Python checks.

The proposed event records:

- Operation ID.
- Reservation ID.
- Attempt ID.
- Connector and provider-account binding.
- Contract-version binding.
- An immutable provider-key fingerprint or reference.
- Event identity and server-side timestamp.

No provider call may begin until this event is
successfully committed and positively acknowledged
by the trusted execution service.

If committing the fence times out or has an uncertain
outcome, the worker must HOLD without calling the
provider.

A subsequent observer seeing the fence is not
permission to replay an interrupted attempt.

## 6. Execution boundary

No real-provider adapter is permitted in the initial
implementation.

The first adapter is a deterministic fake provider.

It records received dispatch attempts and independently
observable effects.

The adapter must not accept RecoveryInspection,
RecoveryRequest.retry_authorized, reservation rows,
or caller-supplied actor claims as authorization.

A future trusted service may call it only after:

1. Real worker-service authentication.
2. Operation and tenant authorization.
3. Valid, exclusive committed reservation.
4. Valid durable attempt identity.
5. Confirmed committed dispatch-risk fence.
6. A separately enforced dispatch decision.

The same-process, one-shot execution flow must have
no automatic resend after its external call becomes
uncertain.

Retry and recovery generations are deliberately
out of scope for this first execution slice.

## 7. Crash-state decision table

| Failure point | Durable information | Permitted automatic action |
| --- | --- | --- |
| Before reservation commit | No trusted claim | No dispatch |
| Reservation commit uncertain | Ownership uncertain | HOLD |
| Reservation committed, before fence | Claim exists, no risk fence | HOLD until a trusted controlled continuation is implemented |
| Fence commit uncertain | Dispatch risk uncertain | HOLD, no provider call |
| Fence committed, before provider call | Provider may not have been called | No automatic takeover or replay |
| Provider called, response lost | Effect may have occurred | HOLD, investigate |
| Worker crashes after call | Local outcome incomplete | HOLD, investigate |
| Provider confirms applied | Requires authenticated provenance | Reconcile; never redispatch that effect |

A replay of an existing fence is not a new
dispatch authorization.

The design must reject attempts to turn a stale claim
or a reloaded risk event into an execution capability.

## 8. Provider retry separation

The current pure recovery policy accepts a
retry_authorized boolean.

That boolean is a policy input, not security authority.

A trusted future recovery service must independently
verify:

- Authentic provider evidence and its provenance.
- Exact operation and provider-account binding.
- Complete durable attempt inventory.
- Contract and idempotency-key constraints.
- Required concurrency protection.
- Retry budget and deadline.
- Current authenticated service authority.

An authorized retry, if later supported, needs its own
durable generation and atomic admission contract.

The initial one-shot execution slice authorizes no
retries under any circumstance.

## 9. Database permissions

The existing admission role must not gain worker
execution or attempt-management privileges.

A future execution principal needs an explicit
least-privilege role and reviewed SQL entry points.

The existing append-only admission write guards
must remain intact.

No unreviewed UPDATE or DELETE permissions may be
added to the four foundation tables.

Separate execution history must also be immutable.

Database-enforced ownership must prevent arbitrary
claiming of another tenant or actor's operation.

The current development connection uses safeact_dev;
that is not evidence of production least privilege.

## 10. Required independent verification

The next test slice must independently measure:

- Exactly one committed reservation under contention.
- Rejected competing dispatch attempts.
- No fake-provider call before a durable risk fence.
- No fake-provider call after uncertain fence commit.
- No automatic second call after a simulated lost reply.
- Correct operation, tenant and provider-account binding.
- No replay after crash or apparent claim expiration.
- At-most-one permitted one-shot dispatch attempt
  in the tested execution protocol.
- Preservation of the original admission bundle.
- All existing Phase A and PostgreSQL regressions.

The fake provider's attempted-call count and effect
count must come from its own instrumentation, not
from a recovery decision object.

An at-most-one local dispatch-attempt test is not
a universal exactly-once provider-effect guarantee.

## 11. Existing limitations

Known unresolved SQL limitations:

1. The SQL gate does not enforce canonical JSON encoding.
2. One runtime credential can assert another enabled
   subject in the current reported security boundary.

Additional gaps include:

- No production worker identity.
- No production ownership-claim contract.
- No durable attempt-risk transition implementation.
- No independent retry-authorization service.
- No real transport-level lost-COMMIT test.
- No production provider dispatch interlock.

These limitations prevent a production-readiness claim.

## 12. Evaluation status

SA-EVAL-003 remains NOT_RUN.

The development evaluator remains separate from the
new test-specific design experiments.

Neither the negative-only RecoveryInspection.retry_allowed
property nor this document may be used as the independent
provider retry-enforcement oracle.

No corpus, evaluator, holdout, or production-approval
metadata is changed by this design.

## 13. Design review gate

Before implementation:

- Confirm the reservation and fence identity model.
- Review the exact append-only database constraints.
- Define the authenticated execution authority boundary.
- Define the fake provider and independent oracle.
- Specify commit-loss and crash-injection tests.
- Preserve existing admission and tenant permissions.

Only after these decisions are verified may a narrow
execution-state test implementation begin.

No real provider dispatch is authorized by this document.
