# B3 design before implementation

2026-10-22. M5.10 design written before baseline code; implementation status
is recorded below. All comparisons use local mock providers; no Stripe or model
call is part of this work. STOP 2 remains required before `qual_v1` runs.

Implementation started after this design: `chaos/baselines/recovery.py` has
independent recovery decisions, and `store.py` has ordinary PostgreSQL atomic
intent/audit/outbox admission with exact replay/conflict handling. Their own
tests cover real late CRM application after an empty lookup, bounded native-key
retention, reviewer safety, concurrent admission and lost acknowledgement.
The subsequent [execution slice](b3-execution-plan.md) adds private worker,
relay and reconciler processes over B3's own ordinary tables, token/version
fencing, append-only observations, retry scheduling and account pacing. Its
tests exercise actual PostgreSQL, HTTP mocks, Celery/Redis and worker death.
Workflow/review runtime and evidence adapters remain. These modules are not
registered as comparison arms and produce no baseline qualification result.

## Purpose and source patterns

B3 is a conventional robust implementation that should be capable of matching
SafeAct's safety. A matching result is legitimate. Compare reusable integration,
recorded evidence and review behavior only where measured; do not infer value
from weaker B0/B1 results.

AWS's [idempotent API guidance](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/)
motivates explicit caller intent and refusal of parameter mismatches for reused
identity. Its [transactional outbox pattern](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)
motivates committing intent and publication together, while retaining duplicate
delivery handling. [Stripe's idempotency reference](https://docs.stripe.com/api/idempotent_requests)
motivates stable parameter-bound keys, bounded retention, and treating saved
errors separately from evidence of non-application. These patterns inform this
design; their documentation does not qualify an implementation.

The implementation uses the existing connector profiles and HTTP connectors,
with the same time-scaled q-v2 contracts, provider faults, request deadlines,
retry/investigation budgets, key retention/margin and pacing parameters as
SafeAct. It does not import SafeAct lifecycle SQL, recovery workers, admission
functions, workflow advancer or protected human-decision functions.

## Durable records and process boundaries

Each baseline has a throwaway PostgreSQL schema and private queue within the
owned qualification slot. SQLAlchemy creates ordinary application tables;
there are no protected SQL transition functions. Worker, relay and reconciler
are independently killable OS processes. Harness observation remains read-only
and outside those processes. They cannot read the independent provider ledger.

| Record | Contents and responsibility |
|---|---|
| Intent | Tenant, stable caller intent, exact immutable target/payload/contract, status and monotonically increasing version; unique intent identity |
| Attempt | Ordinal, fencing token, first dispatch time, lease and bounded outcome/evidence; append history retained |
| Outbox | Committed with intent or retry eligibility; delivery identity and relay publication state |
| Evidence | Actual connector response/lookup facts, source and timestamps; absence stays inconclusive |
| Account pacing | Retry not-before, token budget, breaker failures and half-open probe ownership |
| Workflow | Fixed charge/order plan and separate original-linked inverse intent; immutable child bindings |
| Review | Proposal action, displayed version/evidence fingerprint, requester/proposer, expiry, one reviewer decision and repeat-click key |

Admission checks the exact identity and immutable request in one transaction.
Identical replays return the original intent; a changed request refuses. An
acknowledgement loss is recovered by looking up that identity, without creating
another intent or accepting a different payload. Intent plus initial outbox
must commit together. A crash after publishing but before recording publication
may publish again; the worker must tolerate that delivery.

## Claim, fence and recovery

A short transaction claims a ready row with `FOR UPDATE SKIP LOCKED`, increments
its version, assigns a lease and fresh fencing token, and records the claim.
The dispatch fence commits the attempt before a provider call. Transactions
must not remain open during network requests. An optimistic status/version/token
check rejects a stale worker's outcome while retaining its attempt identity for
reconciliation. A lease does not fence the external provider: expiry during a
request is ambiguity, never permission to repeat a non-idempotent action.

| Observed fact | B3 behavior |
|---|---|
| Unclaimed/abandoned pre-fence work | Requeue within the same bounded claim budget |
| Direct success or matching positive lookup | Record applied evidence and complete the same intent |
| Permanent authoritative pre-execution rejection | Confirm failure with the provider's actual rejection |
| Retryable authoritative rejection for every attempt | Retry within budgets; require human approval on a reviewed contract |
| Timeout, connection loss, 5xx, process loss after fence | Preserve ambiguity; reconcile before deciding |
| Empty, unavailable, mismatched or off-contract lookup | Retain ambiguity; never convert absence to definitive failure |
| Active native key with parameter binding and concurrent safety | Allow a bounded retry before first-fence retention minus margin, with a fence-time recheck |
| Expired key or unprotected non-idempotent attempt | Do not dispatch again; continue bounded lookup, then escalate |
| 429 or outage | Honor retry-after, bounded jitter/backoff and account breaker; conserve attempts where possible |
| Committed late positive evidence after escalation | Reopen within the shared contract's bounds and resolve without another dispatch |

Do not reset the first-fence clock on retry or create a new key when its safety
window expires. Account limits are checked before claiming work so a blocked
claim does not consume an attempt. Investigation and post-escalation deadlines
use persisted database time; the observer records host query intervals rather
than assuming host/database clocks are aligned.

## Workflow and human review

Every arm receives the same fixed business plan: charge a shipping fee, create
a replacement order, refund that exact charge/amount only if the charge is
confirmed applied and the order is confirmed not applied. The inverse is a
separate operation with separate identity, evidence and outcome. An uncertain
order never starts a refund. A lost inverse reply is reconciled independently;
an applied but unverified inverse is unresolved, never restored.

Give B3 these workflow semantics rather than withholding an orchestrator that
SafeAct has. Faults must target the actual inverse step and contracts. The
workflow observer/oracle must accept baseline-owned committed rows without
calling SafeAct SQL or trusting a baseline's restoration claim.

F14 uses B3's own conventional version-bound review queue. Investigators propose
lookup-only reverify or a protected retry, never fabricate provider proof. Two
different human reviewers approve the same displayed version concurrently.
An operation row lock and optimistic version/evidence checks serialize decisions;
one commits, a second decision refuses, and the winning key replays. A stale
display after actual new provider evidence refuses and records the refusal.
Requester, proposer and reviewer independence are checked in application code.
Approval permits the requested action; the ordinary retry/lookup safety checks
still apply at execution. The harness must independently join these rows with
actual request intervals and provider effects, as for SafeAct.

F15 is N/A for B0-B3 because they have no guarded agent credential surface.
That cell is excluded from relevant denominators and cannot count as a pass.
F14 is N/A for B0-B2 because they have no human-review feature.

## Weaker baselines and comparison controls

| Arm | Intended mechanisms and limits |
|---|---|
| B0 | Synchronous bounded naive retry; no durable intent or stable-key guarantee; explicitly intentionally incomplete |
| B1 | Durable job and late-ack Celery delivery/retry, no lookup or native-key reuse |
| B2 | B1 plus stable keys on providers that actually guarantee idempotency; CRM/email legitimately retain B1 behavior |
| B3 | Durable intent/outbox, fencing, contract-aware recovery, pacing, workflow and human escalation above |

Before implementing adapters, define how a synchronous B0 request is killed
without killing the qualification observer, and preserve every attempted
admission/uncertain response. A lost B0 process must not gain synthetic durable
intent from the harness. Conversely, an already authorized request must not
disappear from effect attribution merely because its acknowledgement was lost.
Document this boundary and test it before claiming cross-arm F05/F13 results.

## Review and test gate before freeze

- Prove admission replay/conflict, atomic outbox and acknowledgement recovery.
- Exercise all three connector capabilities, concurrent redelivery and reclaim,
  first-fence retention expiry, late commit after empty lookup, and late commit
  after escalation using real HTTP provider behavior and independent observations.
- Test 429/outage conservation, bounded retry/backoff, breaker probe ownership,
  stale outcome writes, accounting of every admitted intent and projection-free
  reconstruction from B3's own records.
- Qualify the full fixed workflow, including wrong inverse binding and unresolved
  compensation counterexamples; preserve separate forward/inverse metrics.
- Exercise actual concurrent/stale F14 decisions and repeat-click replay without
  borrowing SafeAct's protected SQL. Verify before/after audit and effect counts.
- Run controls and development families against all applicable arms. Differences
  require a named mechanism; repair accidental B3 omissions before comparing.
- Audit declared achievable completion, N/A cells and held-out construction from
  contracts. Do not run held-out outcomes for development tuning.
- Freeze source, scenario/timing/contracts/scoring/coverage and budget only after
  review. Announce **FINAL BIG QUALIFICATION RUN** and wait for the author's go.

No baseline performance, safety advantage or outcome count is claimed here.
