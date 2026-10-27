# B3 execution implementation plan, 2026-10-23

This extends the preimplementation [B3 design](b3-design.md). No SafeAct SQL,
existing migration, production execution or frozen qualification is changed.
Keep the sealed boundary evidence historical. Do not register a partial arm.

## Ordinary transactions and fencing

Extend only disposable `baseline_*` tables with leased claims, attempts,
append-only actual observations, retry/lookup scheduling and account pacing.
Claims use row locking with SKIP LOCKED and optimistic status/version checks.
The dispatch fence persists the attempt and its first-fence timestamp before
HTTP. Every retry rechecks evidence, native retention, budget and review policy
at that fence. A fresh local lease never renews a provider key.

Network requests run outside transactions. A stale worker's real response is
retained for its original attempt, but it cannot overwrite a newer job version.
Positive late evidence is resolved by reconciliation, within reopening bounds.
Claim expiry before the fence can requeue within a bounded abandoned-claim
budget. Dispatch expiry becomes UNKNOWN; it cannot directly requeue CRM/email.

## Recovery and pacing

A reconciler reserves a bounded lookup lease before HTTP and records the real
connector answer afterward. A crashed lookup becomes eligible after that lease;
concurrent reconcilers cannot each issue an unbounded lookup. Ambiguous results,
NOT_FOUND, unavailable lookup and parameter mismatches remain inconclusive.
Only normalized authoritative non-application can justify a failure/retry.

Use database time for every lease, deadline, retention and not-before check.
Share qualification profiles and HTTP connectors, not SafeAct recovery code.
Account token buckets and breaker probes gate claims; deterministic backoff
honors Retry-After. Preserve separate initial and post-escalation lookup bounds.
Evidence recording is idempotent across a lost commit acknowledgement, while
new actual lookups retain distinct observations. A caller cannot supply proof.

## Process slice and validation

Add baseline-owned worker, relay and reconciler entrypoints with late-ack Celery
delivery, bounded loop polling and private configuration. They must use only
mock HTTP URLs and disposable schemas. Keep credentials out of command lines,
logs and retained evidence. Whole process groups are stopped before providers
are drained. Broker messages carry identities, never authority or provider data.

Own PostgreSQL/HTTP tests must prove concurrent claim/fence exclusion, durable
dispatch before the provider sees a request, late CRM commit after empty lookup,
expired lease during an in-flight request, lost commit acknowledgement, stale
response retention, retry cutoff, all connector capabilities and pacing.
Process tests kill a real worker after a provider call and before outcome
persistence, then recover through the independent provider ledger.

Workflow orchestration, B3's own authenticated review queue, normalized evidence
adapters, full baseline registration and B0-B2 remain separate required work.
Passing this execution slice does not qualify those cells. All development
stays on the dev corpus; STOP 2 and the author's explicit go remain required.

## Implemented slice

`runtime.py` implements the ordinary worker/relay/reconciler transactions;
`environment.py` provisions fresh schemas and unprivileged application roles;
`processes.py` runs isolated Celery tasks and loops with private configuration.
Process factories construct HTTP/DB clients after the worker fork. Baseline and
SafeAct tasks use private Celery registration so factory creation order cannot
pollute either app's task registry.

Actual lookup completion also exposed a shared native-key evidence bug in
SafeAct: an answer obtained after retention expiry used the pre-request time.
The [completion binding](lookup-completion-binding.md) records the reproduced
counterexample and the shared connector guard. Negative lookup evidence in B3
is additionally bounded by the attempts present when the lookup was reserved.
Due-time scans exclude retired checks, preventing older inactive jobs from
starving newer work. This scheduling check uses synthetic rows only for the
scheduler test; provider-effect tests use actual HTTP and independent ledgers.
