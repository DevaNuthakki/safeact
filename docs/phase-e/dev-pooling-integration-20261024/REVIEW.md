# Runtime connection pooling integration — pre-run paused

The author authorized integration and regression/local latency checks and
explicitly reserved pre-run and big-run authorization for later. Pooling is
now wired into the application's API and execution stack. This packet records
ordinary regressions and local mock measurements only. No frozen smoke,
calibration, held-out execution or full qualification ran. No real Stripe or
paid model calls, staging, commits or pushes occurred.

## What changed

- The API leases native psycopg connections from a bounded four-connection,
  zero-overflow pool with a five-second wait. Health remains database-free;
  readiness checks liveness. SSE releases its lease after every poll and
  re-checks authentication in SQL.
- Each separate worker/relay/reconciler/advancer process owns a fixed-role pool.
  Worker ownership is created lazily after prefork. Serial background owners
  use one connection; the threaded demo gives its worker/relay shared role two
  connections. Application shutdown closes owned pools.
- The common SafeAct adapter's internal admission engine is bounded and uses
  the same reset hygiene. No role, tenant or credential is switched to share
  a pool. No dependency was installed and native connection type guards remain.
- ExecutionWorker, Reconciler and WorkflowAdvancer accept an explicit connection
  context. Existing injected/fresh factories keep their original behavior.
  Fresh, same-role observer/retry connections remain after uncertain commits,
  avoiding nested checkout deadlocks in one-connection pools.
  MCP admission and metric-scrape connections retain their existing fresh path.
- Idle checkout probes liveness outside an application transaction. Returning
  a lease rolls back unfinished work, restores startup session settings and
  authorization, and clears cursors, listeners, temporary tables, sequence state
  and session advisory locks. Prepared statements are preserved across committed
  work; Psycopg's automatic preparation remains enabled. Broken or unresettable
  sessions are discarded. Pool cleanup never proves a business commit rolled back.

Protected SQL, contract definitions, authorization rules, recovery decisions,
and claim/fence/outcome commit ordering are unchanged. There is no automatic
retry of business work in the pool layer. The only retry is a bounded liveness
probe/replacement before a connection reaches application code.

## Final checks

All 1650 unique Python tests passed: 908 Phase A, 710 database/broker integration,
21 isolated security, nine API journeys and two scripted-agent tests. Integration
had no skips. All 37 console unit and 14 browser tests passed; typecheck, lint and
production build passed. [The inventory](test-inventory.json) lists every Python
test ID. [Integration](integration-check.json) and
[surface checks](surface-checks.json) retain actual results and elapsed time.

The new coverage exercises native connection reuse and preparation, bounded
capacity, role isolation, session-state reset and unfinished transaction rollback,
terminated/broken connections, reset failures, actual fork refusal, concurrent
duplicates, lost claim/fence/outcome acknowledgements, ambiguous non-idempotent
email, advancement and shared API lifespan ownership. The complete suite also
exercises the threaded and separate-process stack wiring.

## Corrections found during integration

1. The initial liveness probe used a read transaction and rollback, which clears
   Psycopg's prepared cache. It now probes in autocommit while the connection is
   idle. The cache regression checks repeated committed requests; unfinished
   transactions still roll back normally and may clear the driver's cache.
2. Concurrent TestClient consumers share an app. Closing one lifespan initially
   shut down the other consumers' pool, producing 503s in workflow tests. App
   lifespan ownership is now counted; only the final owner closes its pool, and
   a later app lifespan lazily obtains a new owner. The original workflow tests
   and the new shared-lifespan/restart regression pass.
3. An extra controlled shutdown probe found a lease could complete reset after
   pool disposal and return a live idle session. The first full 709-test pass had
   succeeded before this extra probe. Return is now serialized with close, and a
   late lease is invalidated. The deterministic regression checks both the closed
   native connection and removal of its actual PostgreSQL backend. All 17 pool
   integration tests and the entire final 710-test integration discovery pass.
4. The first Phase A invocation's signer socket was sandbox-blocked. The complete
   socket-enabled invocation passed all 908. No test was skipped or loosened.

Original failure logs, the initial successful full pass, the shutdown diagnostic
and its exact pre-fix source are retained. They are not substituted for final
results. [Log metadata](regression-log-files.json) preserves original digests and
shared-log 64-hex redaction counts. No API/runtime error body was made less strict.

## Local latency through the integrated implementation

| Concurrent requests | Path | Median ms | p95 ms | Completed requests/s |
|---:|---|---:|---:|---:|
| 1 | Fresh connections | 96.79 | 119.58 | 10.04 |
| 1 | Integrated pooling | 44.67 | 59.55 | 21.47 |
| 4 | Fresh connections | 131.00 | 148.33 | 25.96 |
| 4 | Integrated pooling | 67.31 | 79.00 | 50.13 |

1 concurrent request(s): median 53.9% lower with integrated pooling; 4 concurrent request(s): median 48.6% lower with integrated pooling.

These are 400 measured local operations plus 128 excluded warmups, with zero
errors and exactly one provider call/effect per operation. Every admitted
operation independently reads back as SUCCEEDED. The comparison calls the real
ExecutionWorker.execute with the integrated connection scope, not the earlier
prototype subclass. Liveness probes and full reset costs are included. Both
paths retain the same connector, payload shape, commits, profile and mock HTTP
provider. Mode order alternates across ten blocks. Native backend IDs show
connection reuse independently of elapsed time.

The fresh path uses the existing NullPool admission fixture and fresh worker
connections. The integrated path uses the bounded admission engine and actual
ConnectionPool. These contemporaneous measurements must not be combined with
earlier separate-session medians. Public API/authentication and Redis/Celery/
relay scheduling are excluded, so this is not full application latency or
sustained production capacity. One hundred samples/configuration cannot prove a
stable p99. No latency SLO or sub-10-ms guarantee was assumed.

B0–B3 implementations were not modified. Their connection strategies differ;
this is a SafeAct connection-strategy comparison, not cross-arm performance
evidence. The application's ordinary functional journeys are covered separately
by the API, agent, process-stack and browser regressions.

See [protocol](latency/protocol.json), [summary](latency/summary.json),
[samples](latency/samples.jsonl), [independent provider ledger](latency/provider-ledger.json)
and [cleanup proof](latency/cleanup.json).

## Source amendment and remaining authorization

The exact pre-change tree matched the earlier sealed 470-file source snapshot
plus four base references and its passing 1626 Python / 51 console baseline.
That proof is retained in [baseline.json](baseline.json).

The old frozen candidate is archived in [previous-inputs](previous-inputs/).
[The amendment](source-amendment.json) verifies that only backend source hashes
changed. All scenario, contract, timing, generator, oracle, coverage, applicability
and scoring input digests remain identical. The candidate still has 1200 cases,
60 controls, 120 held-out combinations, five arms and an 80-case sample. Its
review status still awaits the later pre-run review. No execution ledger exists.

[source-sha256.json](source-sha256.json) and [source.tar.gz](source.tar.gz) capture
the current implementation/tests/docs; four unchanged synthetic secret-shaped
fixture files retain exact base-commit references. All 519 artifacts in twelve
earlier sealed packets verify unchanged. [Artifact hashes](artifact-sha256.json)
seal this packet and all shared evidence passes the scanner.

**Pre-run and big-run execution remain paused.** This integration does not
authorize either. The author will explicitly say when pre-run may begin; final
qualification still requires its prominent announcement and explicit go.
