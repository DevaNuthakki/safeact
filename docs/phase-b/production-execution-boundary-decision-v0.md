# SafeAct — Production Execution Boundary Decision v0

Status: PROPOSED FOR REVIEW — NOT APPROVED.
Scope: Durable execution authority and PostgreSQL security.
Baseline: 1041e8a.

This document is a design decision record only.

It grants no dispatch authority and does not authorize
a production migration, deployment, or external effect.

## 1. Verified engineering evidence

The existing repository has demonstrated:

- Durable, authenticated PostgreSQL intent admission.
- Append-only initial operation, audit and outbox records.
- Authenticated negative-only recovery observation.
- One committed experimental reservation under contention.
- Immutable experimental operation-bound risk fences.
- One-time fake-provider invocation under tested faults.
- Rejection of tested forged and wrong-audience tokens.
- Independent fake-provider call and effect counting.
- Preservation of the existing evaluation lifecycle.

The invocation implementation is test-only.

The test experiments use the local development database
role and disposable, isolated schemas.

They do not establish production database permissions.

## 2. Security limitations blocking promotion

The currently reported SQL security limitations are:

1. The SQL gate does not enforce canonical JSON encoding.
2. One runtime credential can assert another enabled subject.

Additional unverified boundaries:

- Production worker authentication and revocation.
- Database-enforced worker-service authority.
- Tenant and operation isolation for execution.
- Least-privilege execution database credentials.
- Trusted attempt inventory and retry authorization.
- Stale-worker and credential-compromise behavior.
- Real network-level uncertain-COMMIT recovery.
- Real provider idempotency and effects.

No production-readiness claim is permitted.

## 3. Separate authority domains

Admission actor tokens and worker-service credentials
MUST remain separate.

Proposed worker authentication:

- Explicitly configured trusted issuer.
- Dedicated worker-service audience.
- Trusted signing keys and key identifiers.
- Verified subject and token lifetime.
- Server-controlled worker authorization records.
- Explicit operation and tenant assignment.
- Fail-closed handling of disabled or unknown workers.

These checks must run before execution-state mutation.

An authenticated token is not automatically a grant to
execute any operation.

Caller-supplied worker labels, actor IDs and permission
flags are never proof of database authority.

The admission service receives no execution capability.

## 4. Database enforcement requirement

A separate execution database principal is required.

It must not reuse the unrestricted development role
or inherit admission-writer privileges by default.

The existing admission role must retain its current
limited permissions.

The future execution role should obtain only reviewed,
minimum privileges needed for its state transitions.

Direct arbitrary INSERT, UPDATE and DELETE privileges
over the admission tables are not acceptable.

Any proposed privileged SQL function must have:

- A reviewed caller-role allowlist.
- A fixed and safe search_path.
- No dynamic SQL based on untrusted identifiers.
- Explicit object qualification.
- Strict operation and tenant identity binding.
- Trusted origin for service identity.
- Independent effective-privilege tests.
- Failure behavior that cannot authorize dispatch.

A SECURITY DEFINER function is not inherently safe.

A function that trusts an arbitrary caller-supplied
worker_subject remains vulnerable to identity spoofing.

A single shared credential cannot be treated as
independent proof of multiple worker identities.

Credential compromise and direct SQL access must be
covered by the final threat model.

## 5. Immutable state model

The design distinguishes these identities:

- operation_id: immutable admitted effect intent.
- claim_id: exclusive one-shot reservation.
- attempt_id: durable prepared attempt.
- fence_id: committed MAY_HAVE_DISPATCHED risk boundary.
- invocation_id: consumed one-time invocation opportunity.

All identities must be bound to one original operation.

The database must reject cross-operation references.

Initial execution permits only one invocation generation.

No automatic lease expiry or claim takeover is included.

Re-observing an existing fence or invocation record
does not provide a new execution opportunity.

Original admission records remain append-only.

Execution history must also remain immutable.

## 6. Invocation ordering and uncertainty

Proposed single-use sequence:

1. Verify the worker service principal.
2. Resolve trusted worker authority.
3. Verify the original admitted operation.
4. Verify claim, attempt and fence bindings.
5. Atomically consume the one-time invocation.
6. Receive positive acknowledgment of the COMMIT.
7. Invoke the deterministic fake provider once.
8. Independently record the observed test result.

No provider call may begin before step 6 succeeds.

If the consumption COMMIT result is uncertain,
the caller must HOLD without invoking the provider.

If a process crashes after consumption, no replay
or automatic new invocation is permitted.

If the fake provider applies an effect but its reply
is lost, the operation must not be dispatched again.

A returned invocation row is not a reusable bearer
credential or cross-process execution token.

The trusted service must not expose an API that lets
a caller request another provider call merely by
presenting a previously consumed invocation ID.

## 7. Safety boundaries and failure model

The tested one-time protocol intentionally favors
at-most-one attempted fake dispatch over liveness.

Safety comes with a known tradeoff:

A crash may permanently block automatic progress.

This does not imply the provider effect was applied.

This does not imply it was rejected.

A missing provider lookup or expired worker ownership
cannot establish non-application.

No retry is permitted by this initial execution slice.

Future recovery requires separately trusted provider
evidence, complete attempt coverage, and a durable
authorized retry-generation protocol.

The existing pure recovery policy's retry_authorized
boolean is not a standalone authorization credential.

## 8. Independent verification plan

Before promoting experimental code, verify:

- Worker and admission audiences cannot be exchanged.
- Invalid signatures and expired tokens deny before SQL.
- An enabled but unauthorized worker is denied.
- Disabled workers cannot claim new execution authority.
- Cross-tenant and cross-provider attempts are rejected.
- The operation and payload identities remain bound.
- SQL role privileges are independently inspected.
- A compromised admission principal cannot execute.
- Multiple contenders yield at most one consumption.
- A transaction rollback yields no dispatch opportunity.
- An uncertain COMMIT yields no fake-provider call.
- A repeated request yields no second call.
- A stale worker cannot obtain fresh invocation rights.
- A crash after consumption causes HOLD.
- A lost provider response causes HOLD.
- Fake provider attempted-call and effect counts
  are observed independently from gate decisions.
- All existing regressions remain green.

Tests must not derive the expected provider count from
the same gate decision being tested.

A test harness cannot grant itself authorization
by setting retry_allowed or dispatch_enabled flags.

## 9. Implementation sequencing

First candidate slice:

A deny-only worker-service authorization component
with typed operation-binding contracts and unit tests.

Second candidate slice:

A disposable PostgreSQL exercise of a separately
restricted execution principal and immutable
invocation-state transitions.

Third candidate slice:

A deterministic fake-provider adapter reachable
only through the reviewed execution boundary.

Each slice needs independent negative tests,
fault injection and preservation of prior regressions.

No production migration or provider integration
is approved by this record.

## 10. Evaluation and release gate

SA-EVAL-003 remains NOT_RUN.

The development evaluator remains:

4 PASS, 6 NOT_RUN.

This design does not change the evaluation corpus,
runner, scenario verdicts, or oracle implementations.

Final holdout remains false.

Production approval remains false.

A passing fake-provider test does not demonstrate
exactly-once real-provider effects.

Approval for real provider integration requires
separate review of security, idempotency, evidence
provenance, crash recovery and authorization.

## 11. Open review decisions

Still to decide and verify:

1. How worker service credentials are issued and
   revoked in a deployed environment.
2. How execution authorization binds to the exact
   operation without relying on caller assertions.
3. The precise new SQL role and grant matrix.
4. Whether a narrowly scoped SQL function can enforce
   the necessary identity boundary safely.
5. The production append-only event schema.
6. Whether stale workers require a future fencing
   or recovery-generation mechanism.
7. How to demonstrate real COMMIT uncertainty
   without confusing it with synthetic ack loss.
8. How independent evaluation will distinguish
   provider-effect proof from admission evidence.

This document remains PROPOSED until review.
