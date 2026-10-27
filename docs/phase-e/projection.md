# Current-state projection (M5.1, migration 0017)

**Status:** done. This closes the Phase B "full-log scans" known limit (`docs/phase-b/phase-b-exit-evidence.md`, Known limits) before any load test, as planned there.

## What it is

`safeact_state_projection` holds one row per operation:
- before any transition, the row is RECORDED at seq 0;
- after that, it holds the latest transition's seq, state, claim, attempt, actor, lease and time.

`safeact_operation_transitions` (the log) is still the source of truth. The projection is derived from it and can always be rebuilt from it.

| Rule | Where it is enforced |
|---|---|
| Written only on the protected path, in the same transaction | An `AFTER INSERT` trigger on `safeact_operations` seeds the RECORDED row. This covers every admission path: product API, workflow, compensation, atomic gate, ORM submission. `safeact_lifecycle_append` advances the row right after inserting the transition. |
| A write must equal the log | A guard trigger refuses (SAT29) an insert that is not RECORDED/seq 0 with no transitions. It refuses an update unless it advances exactly one seq to the log row with that seq, column by column. It refuses every delete. The superuser and the function owner are refused too, which is tested. |
| A log that moves without the projection stops work | `safeact_lifecycle_lock` still reads the log under the row lock. When the projection's seq differs from the log's, it raises SAT29, so no protected write can proceed on a drifted operation (tested). |
| Readers | The 0003 view `safeact_operation_current_state` keeps its exact columns and types and now reads the projection. Every frozen function that reads the view uses it without a text change. The claimable, relay and in-state scans use the projection's `(state, operation_id)` index instead of anti-joining the log. |
| Privileges | The owner gets SELECT, INSERT, UPDATE on the projection; the guard still applies to it. Callers, the API, the MCP role and the admission writer get nothing. The seed trigger function is SECURITY DEFINER (`TRIGGER_FUNCTIONS`), never granted, and PostgreSQL refuses to call it outside a trigger. |

**Migration:**
- The upgrade takes a SHARE lock on the log, backfills from it, then creates the triggers and replaces the functions and the view, all in one transaction.
- The downgrade restores every replaced text and the 0003 view, then drops the projection. The log is never touched.
- `test_migration_0017_roundtrip` proves backfill equality on history written at 0016, byte-identical functions and view after downgrade, and an unchanged log.

## Proof that it always matches the log

- **`storage/state_projection.projection_drift`** recomputes each operation's latest transition straight from the log, the same way as the 0003 view, and returns every operation whose projection row is missing or differs.
- **It runs before every disposable schema is dropped:**
  - the lifecycle fixture and the API fixture (all integration classes);
  - the API E2E journeys;
  - the agent-evaluation harness.

  A drift fails the class.
- **`tests/integration/test_state_projection.py`:**
  - every driven state;
  - refused calls leave the projection unchanged;
  - the guard refuses each kind of bad write, as the superuser and as the owner;
  - the lock stops on drift;
  - scans equal the old log queries;
  - concurrent claims (6 connections × 4 operations: exactly one winner each);
  - Hypothesis randomized programs: 25 derandomized programs of up to 14 legal, illegal and stale calls over 3 operations, with drift checked after every call.
- **Fixtures that write the log directly on purpose** (`seed_history`, the drift test) call `catch_up_projection`. It applies only guard-accepted single-seq steps.

## Before and after

**Setup:**
- Measured with `measure_lifecycle_scans`, which now reports both the old log queries (`log_*`) and the projection queries in one run.
- PostgreSQL 17.11 in Docker on macOS 15.4.1, Intel Core i7-8850H (12 threads), 32 GB.
- 20,050 operations and 60,100 transitions; median of 7 `EXPLAIN ANALYZE` runs after a warm-up.
- Raw output: [`projection-scans-v1.json`](projection-scans-v1.json).

| Query | Log (0003/0007) | Projection (0017) | Plan now |
|---|---|---|---|
| Claimable (limit 100) | 21.4 ms | **0.26 ms** | projection state index, PK join |
| DISPATCHING scan (limit 100) | 40.8 ms | **0.13 ms** | projection state index |
| UNKNOWN scan (rare state) | 0.09 ms | 0.02 ms | projection state index |
| Current state of one operation | — | 0.03 ms | two PK index scans |

The Phase B figures (26.4 ms and 52.6 ms) came from the same setup on an earlier day. The log figures above are from the same run as the projection figures, so the two columns are directly comparable.

**Plan assertions** (`test_lifecycle_indexes`, 20,000-operation history):
- every state scan, CLAIMED and DISPATCHING included, uses `ix_safeact_state_projection_state` and never reads the transition log;
- the claimable scan never sequentially scans the log or the projection.

## Deviation from the Phase B note

The Phase B note planned **partial** indexes on the projection. 0017 uses one full `(state, operation_id)` index instead, for two reasons:
- recovery and accounting also scan terminal states;
- an equality lookup on `state` already touches only that state's entries.

Index size grows with total operations, not with pending work. That is acceptable at this scale and revisited by the load test (M5.13).

## Known limits

- `TRUNCATE` is not guarded by a trigger. It is the same as the other lifecycle tables: no runtime role holds TRUNCATE, and only the schema owner or superuser could.
- The projection adds one UPDATE and one guard lookup (a PK probe on the log) to every append, and one PK read to every lock. The load test measures the cost under contention.
