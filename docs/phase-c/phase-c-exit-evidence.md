# Phase C exit evidence — verification and bounded repair (Milestone 2)

Status: Phase C complete for three **mock** connector profiles (payments,
CRM, email), the fixed two-step workflow and its compensation, and a real
Celery + Redis broker adapter. Disposable schemas, in-process provider
mocks and local compose services only. Production execution remains
**not approved** and deny-only: the public schema registers no connector
contract, so nothing can pass the dispatch fence there.

Blueprint references: §03 (identity, connector contracts), §04 (decision
rules), §05 (durable state, delivery and commit uncertainty), §08 (fault
catalog), §11 (Phase C exit: "F01–F13 exercise correct outcomes";
acceptance checks A3, A5, A6).

## What this does and does not claim

Claimed, within the declared tested contracts (the three mock profiles
in `core/connector_profiles.py`, their test-scaled versions in
`tests/integration/profiles_fixture.py`, and the local Redis broker):

- every fault family F01–F13 is exercised for every profile it applies
  to, with the outcome the §08 catalog requires (matrix below);
- zero observed prohibited duplicate effects across all of them, counted
  by each mock provider's harness-only effects ledger, never from what
  SafeAct reports;
- every admitted operation stays accounted for; unresolved escalation is
  reported apart from completed work, and a definitive rejection awaiting
  a retry decision apart from genuine ambiguity (A5);
- ambiguous non-idempotent work never receives an automatic retry, and
  the database refuses one even if the reconciler is bypassed (A3);
- compensation is a separately identified, verified operation, and
  forward, failed and inverse histories all stay visible (A6).

Not claimed: universal exactly-once; behaviour of a real provider (the
Stripe sandbox adapter is Phase D); remote fencing beyond what a native
idempotency key gives; F14–F16 (Phase D); authenticated JWT-to-session
binding (open gate); any production availability or operating history.

## Reproduce

```
docker compose --env-file .env -f deploy/compose.yaml up -d
.venv/bin/python -m unittest discover -s tests/phase_a -t . -p 'test_*.py'
SAFEACT_RUN_DB_INTEGRATION=1 SAFEACT_RUN_DISPOSABLE_DB_TESTS=1 SAFEACT_RUN_DB_SECURITY_TESTS=1 \
  SAFEACT_RUN_BROKER_TESTS=1 \
  .venv/bin/python -m unittest discover -s tests/integration -t . -p 'test_*.py'
SAFEACT_RUN_ISOLATED_CLUSTER_TESTS=1 \
  .venv/bin/python -m unittest discover -s tests/isolated -t . -p 'test_*.py'
.venv/bin/python -m deploy.evaluation.run_corpus_v1
```

`.env` (mode 600) holds the local database and Redis credentials.
Python dependencies: `requirements-db.lock`, `requirements-broker.lock`.

Result at exit: 655/655 tests pass (315 Phase A, 327 PostgreSQL and
broker, 13 isolated) with the flags above and no skips. Dev evaluator
unchanged: 4 PASS / 6 NOT_RUN. Dev database at `0010_compensation`
with no execution history and no registered contracts; the
admission-role provisioning checks pass at that revision.

## Components

| Concern | Location |
|---|---|
| Connector profiles (contract every decision is bound to) | `core/connector_profiles.py` |
| Server-side provider keys | `core/provider_keys.py` |
| Lifecycle states, legal pairs, reason vocabulary | `core/lifecycle.py` |
| Workflow status model | `core/workflow.py` |
| Frozen per-migration SQL (pinned by digest) | `storage/lifecycle_sql/v0003.py` … `v0010.py` |
| Head view of the chain (fixtures, inventory, grants) | `storage/durable_lifecycle_sql.py` |
| Connector port, contract-aware lookup normalization | `connectors/base.py` |
| Adapters / mock providers / harness ledger | `connectors/{payments,crm,email}.py`, `connectors/*_mock.py`, `connectors/ledger.py` |
| Worker, reconciler | `storage/experimental_durable_execution.py`, `storage/experimental_recovery_worker.py` |
| Workflow creation and advancement | `storage/experimental_workflow.py` |
| Celery + Redis adapter | `storage/experimental_celery.py`, `deploy/compose.yaml` |

## Migrations (each frozen after its commit)

| Revision | Adds | New SQLSTATEs |
|---|---|---|
| 0004_connector_contracts | Immutable, versioned contracts; fence refuses an unregistered contract | SAT10 |
| 0005_deadlines_reopen | SQL investigation deadline; no "verify later" after it; evidence-gated reopen | SAT11, SAT14 |
| 0006_claim_exit | History-checked exits from CLAIMED; owner release | — |
| 0007_same_key_retry | SQL retry guard (`safeact_retry_basis`) at decision and fence; attempt and abandoned-claim budgets; UNKNOWN reason vocabulary | SAT12, SAT13 |
| 0008_keys_escalation | Server-derived provider keys (CHECK + unique index); RETRY_ELIGIBLE → ESCALATED_UNRESOLVED; evidence-triggered reopen | — |
| 0009_workflow | Fixed two-step workflow, advancer role, derived status | SAT15 |
| 0010_compensation | Separately identified, verified compensation | SAT16 |

0003 was moved into `storage/lifecycle_sql/v0003.py` with its golden
digest captured first and proven byte-identical after the move
(`tests/phase_a/test_migration_0003_frozen.py`); every later migration
is pinned by `tests/phase_a/test_migration_sql_frozen.py`. Each has an
upgrade/downgrade/upgrade round trip on a throwaway database, a
downgrade that refuses while history depends on it, and an autogenerate
guard (`tests/integration/test_migration_00NN_roundtrip.py`).

## F01–F13 × profile

P = payments, C = CRM, E = email. Test files are under
`tests/integration/`; "faults" is `test_connector_profile_faults.py`,
"retry" is `test_same_key_retry.py`, "crash" is
`test_durable_execution_crash_recovery.py`.

| Fault | Required observation (§08) | P | C | E |
|---|---|---|---|---|
| F01 effect commits, response lost | One effect; evidence-based success or unresolved | faults `test_f01_payments_resolves_from_lookup` → SUCCEEDED | faults `test_f01_crm_resolves_when_record_is_searchable` → SUCCEEDED; `test_f01_crm_unsearchable_record_escalates_never_fails` → ESCALATED; `test_deadlines_reopen` `test_late_commit_after_escalation_reopens_to_success` | faults `test_f01_email_escalates_at_once_and_is_never_resent`; `test_keys_and_escalation` `test_email_late_success_reopens_on_recorded_evidence_only` |
| F02 rejected before execution | Retry only on definitive evidence | retry `test_f02_payments_retries_once_after_proof`, `test_f02_permanent_rejection_is_never_retried` | retry `test_f02_crm_retries_once_on_recorded_proof_despite_eventual_search`, `test_f02_without_retry_authorization_failure_is_confirmed`, `test_attempt_budget_ends_in_confirmed_failure` | faults `test_definitive_rejection_is_failed_with_no_effect` (max_attempts 1: never retried) |
| F03 5xx after commit | UNKNOWN, then verification; no status-code shortcut | faults `test_f03_error_after_commit_is_unknown_then_verified` (all three profiles; reason `ambiguous_error_after_send`) | ← | ← |
| F04 duplicate delivery / concurrent submission | One operation; provider protections respected | faults `test_f04_duplicate_delivery_has_one_claimant_per_profile` (all), `test_f04_resubmission_is_one_operation_and_changed_payload_conflicts` (C, E); happy path `test_concurrent_workers_on_one_task_have_one_claimant`; retry `test_eight_concurrent_reclaims_have_one_owner`; `test_workflow` `test_concurrent_creates_make_one_workflow`, `test_concurrent_advancers_admit_one_order` | ← | ← |
| F05 worker dies after dispatch | Reconciler investigates before any redispatch | faults `test_f05_worker_death_after_dispatch_is_investigated_not_redone` (all; one call each) | ← | ← |
| F06 lease expires while the request runs | No unprotected second dispatch; late completion handled | retry `test_f06_payments_same_key_retry_is_absorbed_by_the_provider` (same key, provider refuses in-flight duplicate; late reply recorded) | retry `test_f06_crm_lease_expiry_never_redispatches` | retry `test_f06_email_lease_expiry_escalates_and_never_resends` |
| F07 lookup empty before late commit | Empty result never becomes confirmed rejection | faults `test_f07_payments_in_flight_lookup_is_not_proof` | faults `test_f07_crm_empty_search_before_late_commit`, `test_f07_crm_late_commit_after_deadline_stays_escalated`; reopen in `test_deadlines_reopen` | faults `test_f07_email_late_delivery_stays_escalated` |
| F08 Redis lost / broker redelivery | Durable intent survives; coordination recovers | `test_celery_broker`: `test_redis_loss_never_loses_an_admitted_operation`, `test_unacknowledged_message_is_redelivered_after_consumer_death`, `test_duplicate_publishes_have_one_claimant`, `test_relay_through_redis_executes_each_operation_once` (payments; the broker path is connector-independent) | | |
| F09 key retention expires | Replay blocked or reconciled under a different contract | retry `test_f09_retention_lapse_before_the_fence_blocks_the_replay`, `test_fence_just_before_cutoff_sends_inside_retention`; Phase A `test_connectors` `test_key_retention_boundary`, `test_strong_absence_after_key_retention_is_not_proof` | n/a (no key) | n/a (no key) |
| F10 same key, different payload | Conflict; no reuse of a prior outcome | admission: `test_intent_submission`, `test_lifecycle_transitions` `test_changed_payload_key_reuse_is_rejected`; provider keys: faults `test_second_operation_can_never_reuse_a_provider_key`, `test_keys_and_escalation`; adapter: Phase A `test_same_key_replays_and_changed_parameters_conflict` | faults `test_f04_resubmission_is_one_operation_and_changed_payload_conflicts` | ← |
| F11 commit acknowledgement lost | Recover by identity; no blind reinsertion | crash `test_lost_claim_ack_stops_without_dispatch`, `test_lost_outcome_ack_is_observed_not_rewritten`, `test_connection_drop_mid_outcome_commit_repeats_write_once`, `test_reconciler_lost_ack_resumes_on_next_pass`; `test_retry_fence_ack` `test_lost_retry_fence_ack_never_dispatches`; `test_workflow` `test_lost_advance_ack_admits_the_order_once`, `test_create_is_idempotent_and_refuses_a_changed_plan`; `test_compensation` `test_lost_ack_and_concurrency_admit_one_compensation` | ← (worker and workflow paths are connector-independent) | ← |
| F12 lookup fails or lies outside contract | UNKNOWN and escalation, not invented success | crash `test_unavailable_lookup_is_inconclusive_not_failure`; Phase A `test_found_record_must_bind_to_this_payload`, `test_in_progress_unavailable_and_unknown_status_are_inconclusive` | faults `test_f12_crm_unavailable_search_escalates_without_inventing`, `test_f12_crm_search_returning_other_data_is_not_success` | no lookup by contract (`no_lookup`) |
| F13 compensation times out / fails | Separate uncertain compensation; no false restoration | `test_compensation`: `test_f13_compensation_timeout_is_verified_not_assumed`, `test_f13_unverifiable_compensation_stays_unresolved`, `test_f13_rejected_compensation_is_unresolved_never_restored` | (order step; never compensated when escalated: `test_never_compensated_before_a_confirmed_order_failure`) | n/a |

## Acceptance checks

**A3 — no unsupported automatic retry of ambiguous non-idempotent work.**
Policy: email escalates at once even with retry authorized (faults
`test_f01_email_escalates_at_once_and_is_never_resent`, retry
`test_f06_email_lease_expiry_escalates_and_never_resends`). Database: a
direct VERIFYING → RETRY_ELIGIBLE by the recovery role is refused for
email and CRM without proof, and for any operation with an applied
effect (retry `test_sql_refuses_unprotected_retries_directly`); the guard
is rechecked at the fence for every attempt after the first (retry
`test_f09_retention_lapse_before_the_fence_blocks_the_replay`).

**A5 — every accepted intent accounted for; unresolved ≠ completed.**
Every fault test ends with an invariant check (accounting total equals
admitted; at most one effect per operation from the ledger).
`safeact_accounting()` splits UNKNOWN/VERIFYING by reason
(`retry_pending` vs ambiguous): retry `test_f02_payments_retries_once_after_proof`.
Unreclaimable retry work goes to ESCALATED_UNRESOLVED with its reason
instead of being parked (`test_keys_and_escalation`
`test_abandoned_claim_limit_escalates_for_review`,
`test_unregistered_contract_escalates_on_exhausted_budget`).

**A6 — compensation history and uncertainty remain visible.**
`test_compensation` `test_confirmed_order_failure_refunds_exactly_the_charge`
asserts charge SUCCEEDED, order FAILED_CONFIRMED and refund SUCCEEDED all
remain visible, with ledger effects counted separately (1 forward, 0
failed forward, 1 compensation); the F13 tests show uncertain or failed
compensation as COMPENSATION_UNRESOLVED, never restored.

## Deliberate deviations from the §05 diagram

Each is a legal pair in `core/lifecycle.py`, seeded by its own migration,
and guarded inside the protected SQL append function:

| Pair | Why | Guard |
|---|---|---|
| VERIFYING → FAILED_CONFIRMED (0003) | §09 confirmed permanent failure, F02 | Proof of non-application for every attempt, no applied evidence (SAT09) |
| ESCALATED_UNRESOLVED → VERIFYING (0005) | Escalation notifies a human; it is not abandonment | A decisive evidence row recorded after this escalation (SAT09), within `max_reopens` (SAT14); a reopened operation cannot "verify later" |
| CLAIMED → RETRY_ELIGIBLE / CLAIMED → UNKNOWN (0006) | Safe exit for an abandoned claim; nothing is dispatched while CLAIMED | The exit is chosen by the attempt history (SAT01); recovery only after the claim lease (SAT04) |
| RETRY_ELIGIBLE → ESCALATED_UNRESOLVED (0008) | Retry work that can never run goes to human review | Only when `safeact_reclaim_blocked()` reports `abandoned_claim_limit` or `attempt_budget_exhausted`, with that reason (SAT01) |

## Known limits

- **Request deadline vs delivery.** The request deadline bounds when the
  worker *sends and stops waiting*, not when the network *delivers* the
  request. A request already in flight may arrive slightly later; the
  contract's key safety margin (≥ the request deadline, enforced by a
  CHECK) must absorb that delivery delay. The tests show the retry
  reached the provider inside retention on the mock's own clock.
- **Trusted STRONG lookup.** A lookup the contract declares STRONG is
  trusted inside key retention. If such a provider falsely reports
  absence and no contradicting receipt was recorded, SafeAct cannot
  detect it and may record a confirmed failure or a proof-based retry.
  Lookups that contradict a recorded receipt, bind to other parameters,
  or come from an EVENTUAL/UNAVAILABLE contract are never decisive.
- **Mock receipt clock.** Provider-side times (retention, receipt time)
  come from each mock's monotonic clock, not a real network or provider
  clock. Real-provider timing is Phase D (sandbox adapter).
- **Broker redelivery cadence.** kombu's Redis transport restores an
  unacknowledged message when a consumer starts after its visibility
  timeout, and otherwise only about every 100 s (every 10th check of a
  10 s cycle). Redelivery is guaranteed eventually, not promptly; the
  claim makes any duplicate harmless, and the reconciler, not the
  broker, recovers in-flight work from the database.
- **Visibility timeout ≥ task time.** If a task outlives the visibility
  timeout Redis redelivers it while it still runs; the duplicate is
  refused by the claim but wastes a delivery. Production setting must
  exceed claim lease + request deadline.
- **Celery worker in tests runs in-process** (so the harness ledger is
  readable); worker loss is simulated by a consumer process that takes a
  message and dies without acknowledging it.
  **Closed 2026-10-21 (M5.5):** `tests/integration/test_worker_process_kill.py`
  SIGKILLs a real, separate prefork Celery worker process at four points
  (after the claim, after the provider applied the effect, before the
  outcome commit, and a prefork child while its parent lives) over HTTP
  mock providers; the ledger is read through the harness-only admin port.
  One effect and one provider call in every case.
- **Workflow creation authorization.** The plan (including the
  compensation policy) is recorded at creation, but binding creation to
  an authenticated, authorized requester for both steps is the M3 API.
- **Full-log scans** (from Phase B) remain: the derived current-state
  projection is still planned before the evaluation milestone.
  **Closed 2026-10-20 (M5.1):** see `docs/phase-e/projection.md`.
- **Abandoned-claim cap** is a fixed 3 in SQL, not per contract.
- **Dev evaluator** unchanged at 4 PASS / 6 NOT_RUN; the corpus remains
  design-stage.

## Deferred, not reduced

- Real payments sandbox adapter, F14–F16, product console and MCP:
  Phase D (Milestones 3–4).
- Derived current-state projection: before the evaluation/load-test
  milestone.
- 1,000+ seeded scenarios vs baselines B0–B3: Phase E.
- JWT-to-database-session binding: open gate.
