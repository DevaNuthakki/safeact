# Phase B exit evidence — durable execution (Milestone 1)

Status: Phase B vertical slice complete for the **mock payments refund**
profile. Disposable-schema and in-memory provider only. Production
execution remains **not approved** and deny-only.

Blueprint references: §05 (durable state, concurrency, architecture),
§11 (Phase B exit: "happy path plus crash/commit uncertainty behavior";
acceptance checks A1, A2).

## What this does and does not claim

Claimed, within the declared tested contract (one mock refund connector,
STRONG and EVENTUAL lookup, one attempt per operation):

- no provider call happens before a committed intent and a committed
  dispatch-risk fence (A1);
- illegal, skipped, stale and wrong-owner transitions, and changed-payload
  key reuse, are rejected by the database (A2);
- zero observed prohibited duplicate effects across all happy-path,
  crash and commit-uncertainty scenarios, counted by the provider mock
  itself, not by worker reports.

Not claimed: universal exactly-once; remote fencing (the mock does not
participate in fencing, only native idempotency); a real provider; CRM or
email profiles (Milestone 2); authenticated JWT-to-database-session
binding (open gate); multi-attempt retry (Milestone 2).

## Reproduce

```
.venv/bin/python -m unittest discover -s tests/phase_a -t . -p 'test_*.py'
SAFEACT_RUN_DB_INTEGRATION=1 SAFEACT_RUN_DISPOSABLE_DB_TESTS=1 SAFEACT_RUN_DB_SECURITY_TESTS=1 \
  .venv/bin/python -m unittest discover -s tests/integration -t . -p 'test_*.py'
SAFEACT_RUN_ISOLATED_CLUSTER_TESTS=1 \
  .venv/bin/python -m unittest discover -s tests/isolated -t . -p 'test_*.py'
.venv/bin/python -m deploy.evaluation.run_corpus_v1
SAFEACT_RUN_DB_INTEGRATION=1 SAFEACT_RUN_DISPOSABLE_DB_TESTS=1 \
  .venv/bin/python -m deploy.evaluation.measure_lifecycle_scans
```

Result at exit: 489/489 tests pass (262 Phase A, 214 PostgreSQL,
13 isolated), no skips with the flags above. Dev evaluator unchanged:
4 PASS / 6 NOT_RUN (SA-EVAL-003 NOT_RUN; this slice adds no evaluator
adapters).

## Components

| Concern | Location |
|---|---|
| States and legal transitions (single source) | `core/lifecycle.py` |
| Schema, protected functions, grants builder | `storage/durable_lifecycle_sql.py` |
| Migration (public) | `deploy/migrations/versions/0003_durable_lifecycle.py` |
| Typed calls and SQLSTATE mapping | `storage/durable_lifecycle.py` |
| Payments mock (idempotency, lookup, faults) | `storage/experimental_payments_mock.py` |
| Relay + refund worker (runtime-independent ports) | `storage/experimental_durable_execution.py` |
| Reconciler + accounting | `storage/experimental_recovery_worker.py` |
| Disposable fixture (schema, roles, seeding) | `tests/integration/lifecycle_fixture.py` |

Design in one paragraph: the lifecycle is an **append-only transition
log**; an operation's state is its latest row and `seq` is its owner
version. `PRIMARY KEY (operation_id, seq)` makes two workers appending the
same next version impossible. Each transition is reachable through exactly
one `SECURITY DEFINER` function with `search_path = pg_catalog, pg_temp`,
qualified objects, a `FOR UPDATE` row lock, expected-state/seq/claim-owner
checks, and an audit event in the same transaction. Callers hold no table
privileges; EXECUTE is revoked from PUBLIC; the NOLOGIN function owner
holds an exact, tested minimum. Existing 0001/0002 constraints and write
guards are unchanged.

## A1 — no network dispatch before durable intent

| Evidence | Test |
|---|---|
| Every provider call observes, from an independent session, a committed intent, admission audit, outbox event and committed DISPATCHING fence; params equal the durable payload | `test_durable_execution_happy_path.test_provider_sees_committed_intent_and_fence_on_every_call` |
| Unadmitted operation → no call | `…happy_path.test_unadmitted_operation_never_reaches_provider` |
| Uncommitted, then rolled-back, admission → invisible, no call | `…happy_path.test_uncommitted_or_rolled_back_intent_never_dispatches` |
| Fence commit raised before COMMIT, real connection drop, or lost ack after COMMIT → never calls provider | `…happy_path.test_fence_commit_failure_never_calls_provider` |
| Dispatch inputs come from the durable record under the claim lock, not the task | `…happy_path.test_claim_returns_durable_intent_not_task_data`, `test_invalid_durable_intent_is_never_dispatched` |
| Admission writes intent, payload hash, audit and outbox atomically (pre-existing) | `test_submission_commits`, `test_payload_persistence` |

## A2 — illegal/stale transitions and changed-payload key reuse rejected

| Evidence | Test (`test_lifecycle_transitions` unless noted) |
|---|---|
| DB seed equals `core.lifecycle.LEGAL_TRANSITIONS` | `test_seed_matches_core_legal_transitions`, phase A `test_seed_matches_core_transitions` |
| Every illegal recovery pair from every state | `test_every_illegal_recovery_pair_is_rejected` |
| Dispatch outcome only from DISPATCHING | `test_dispatch_outcome_only_from_dispatching` |
| Skipped states (RECORDED→DISPATCHING, RECORDED→SUCCEEDED, CLAIMED→SUCCEEDED, UNKNOWN→SUCCEEDED) | `test_skipped_states_are_rejected` |
| Stale owner version; replay after success | `test_stale_owner_version_is_rejected`, `test_claim_of_claimed_operation_is_stale` |
| Wrong claim owner (other worker, forged claim id) | `test_wrong_claim_owner_is_rejected` |
| PK, FK and CHECK backstops; history immutable | `test_primary_key_rejects_two_rows_for_same_version`, `test_direct_illegal_pair_and_bad_seq_rejected_by_table`, `test_history_rows_cannot_be_updated_or_deleted` |
| 8-way races: one claim owner; one outcome per version | `test_eight_concurrent_claims_produce_one_owner`, `test_eight_concurrent_outcomes_for_same_version` |
| Changed-payload key reuse | `test_changed_payload_key_reuse_is_rejected` |
| Least privilege and function hardening | `test_lifecycle_privileges` (8 tests, incl. temp-table `search_path` shadowing and exact owner privileges) |

## Happy path

`test_durable_execution_happy_path.test_mock_refund_happy_path`: admission
→ relay → claim → fence → mock refund → `confirmed_applied` evidence with
the provider's refund id → SUCCEEDED (seq 3); audit chain
`INTENT_RECORDED, LIFECYCLE_CLAIMED, DISPATCH_RISK_COMMITTED,
ATTEMPT_EVIDENCE_RECORDED, LIFECYCLE_DISPATCH_OUTCOME`; 1 call, 1 effect.
Duplicate delivery and contention: `test_double_publish_produces_one_claim_and_one_effect`,
`test_concurrent_workers_on_one_task_have_one_claimant`.

## Crash and commit-uncertainty behavior

All in `test_durable_execution_crash_recovery`. Every test ends with an
invariant check: at most one effect per operation (mock counters), every
admitted operation accounted for, contiguous transition history. An
ad-hoc mutation run (not a committed test) confirmed the invariant fails
when an accepted operation goes missing.

| Scenario | Outcome |
|---|---|
| Crash after claim | Stays CLAIMED, never republished, reclaimed or dispatched; counted |
| Crash after fence, STRONG lookup | Lease must expire first; lookup proves absence → FAILED_CONFIRMED with proof; 0 calls |
| Crash after fence, EVENTUAL lookup | NOT_FOUND → VERIFY_LATER (UNKNOWN), then ESCALATED_UNRESOLVED at deadline; never FAILED |
| Lookup unavailable | INCONCLUSIVE → UNKNOWN, not failure |
| Crash after provider effect, before outcome commit | Lookup APPLIED → SUCCEEDED; 1 call, 1 effect |
| Lost claim ack | UNCERTAIN, no dispatch |
| Lost outcome ack | Worker observes by identity; no rewrite |
| Real connection drop mid outcome commit | Observe, then one version-checked repeat |
| Drop with failed observation | UNCERTAIN; reconciler later resolves SUCCEEDED; no redispatch |
| Reconciler lost ack | Next pass resumes from the durable state |
| Stale worker reply after recovery decided | Appended as evidence; decision not overwritten; next pass reconciles the receipt |
| RETRY_ELIGIBLE reached | Counted separately; never relayed, claimed or redispatched |
| Mixed run | Accounting total = admitted; completed, unresolved, retry-eligible and in-flight buckets distinct |

## Blueprint §05 traceability

| §05 clause | Where |
|---|---|
| Persist action, payload hash, audit, outbox in one transaction before any network call | `storage/submission.record_intent` (pre-existing) + A1 tests |
| Transactional claim with ownership version | `safeact_claim` (`FOR UPDATE`, seq) |
| Every update checks expected state and owner version, appends audit in same txn | `safeact_lifecycle_append` + per-transition functions |
| Protected transition function, not a CHECK on current state | 0003 functions; FK to legal-pairs table as backstop |
| Expired lease → investigation, not duplicate dispatch | `safeact_recovery_transition` (DISPATCHING→UNKNOWN only after lease) |
| Stale worker response recorded as evidence without overwriting | `RefundWorker._late`, `test_stale_worker_reply_is_evidence_not_overwrite` |
| Local vs remote fencing | Local only: DB fence. Remote: the mock's native idempotency key, not a fence |
| Outbox relay may publish twice | `relay_once` republishes never-claimed work; `safeact_outbox_deliveries` allows many rows |
| After connection drop during commit, query by identity before repeating writes | `RefundWorker._persist`, crash-recovery tests |
| Do not equate exception with rollback | Every commit failure is UNCERTAIN; never a dispatch trigger |
| API → outbox → relay → **Celery worker** | Deferred (see below), behind `Publisher` / `TaskSource` ports |

## Deliberate deviation from the §05 diagram

`VERIFYING → FAILED_CONFIRMED` is legal. §09 reports confirmed permanent
failure and fault F02 needs it. It is reachable **only** with
authoritative non-application evidence: `proven_not_applied` with a
reference for every attempt and no applied evidence, enforced inside the
protected SQL append function (SQLSTATE SAT09), not only in Python. An
empty, NOT_FOUND or INCONCLUSIVE lookup can never reach it
(`test_empty_or_inconclusive_lookup_never_reaches_failed`,
`test_contradictory_evidence_never_reaches_failed`). SUCCEEDED likewise
requires recorded applied evidence. Evidence authenticity rests on the
trusted worker and reconciler roles that may record it; the database
checks presence and consistency, not provenance.

## Known limits

**Full-log scans (accepted for M1).** An append-only log cannot index
"latest state is X". Measured with `measure_lifecycle_scans`, PostgreSQL
17.11 in Docker on macOS x86_64, 20,050 operations / 60,100 transitions,
median of 7 `EXPLAIN ANALYZE` runs after warm-up:

| Query | Median | Plan |
|---|---|---|
| Claimable (never-claimed, limit 100) | **26.4 ms** | Hash anti-join, seq scans of operations and transitions |
| DISPATCHING scan (limit 100) | **52.6 ms** | Merge join over every historical DISPATCHING row |
| UNKNOWN scan (rare state) | 0.14 ms | State index + PK probe |
| Current state of one operation | 0.05 ms | PK index scans |

Both slow queries grow with total history, not with pending work.
**Planned fix, before the evaluation / load-test milestone:** a derived
current-state projection table, written only by the protected functions
in the same transaction as each transition append, with the transition log
remaining the source of truth and a test proving projection and log always
match. Claimable and in-state scans then use partial indexes on the
projection.

**Closed 2026-10-20 (M5.1, migration 0017):** see
`docs/phase-e/projection.md` (claimable 21.4 → 0.26 ms, DISPATCHING
40.8 → 0.13 ms on the same history; one full state index rather than
partial ones, explained there).

**Other limits.**
- CLAIMED after a worker crash has no M1 exit (no legal edge). It is
  visible and counted; nothing was dispatched for it.
- Worker identity is `session_user` of test-only LOGIN roles.
  JWT-to-database-session binding remains open.
- Evidence provenance is trusted-role based (see deviation above).
- 0003 was amended twice before release; it exists only on the local dev
  database, which was refreshed with no history lost. Its downgrade
  refuses while execution history exists.

## Deferred, not reduced

- **Celery + Redis**: deferred to a later milestone and **still required
  in v1** (§05, §10, fault F08). Relay and worker logic depend only on
  `Publisher` / `TaskSource`; a Celery adapter implements those without
  changing the logic. Broker acknowledgements, redelivery, worker-loss
  settings and Redis persistence are specified then.
- **Same-key retry** from RETRY_ELIGIBLE: Milestone 2. The legal pair
  exists; the claim function rejects it (SAT06) until then.
- **CRM and email profiles, reconciliation of late commit / unavailable
  evidence across profiles**: Milestone 2.
