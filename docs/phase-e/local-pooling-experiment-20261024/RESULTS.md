# Local pooling and prepared-statement experiment — 2026-10-24

**Pooling substantially reduced latency in this local comparison. Forcing
immediate prepared statements showed no clear advantage over the driver's
automatic preparation. Sub-10-ms operation latency was not achieved.**

The user authorized the comparison and requested notice before pre-run work.
No pre-run, frozen smoke, calibration or big run executed. No real Stripe or
paid API requests occurred. This is a benchmark-only prototype; it is not
enabled in the application or frozen qualification build. Nothing was staged
or committed, and product source and the frozen input pin remain unchanged.

## Results

| Concurrent requests | Configuration | Median ms | p95 ms | Completed requests/s |
|---:|---|---:|---:|---:|
| 1 | Fresh connections, current preparation default | 133.42 | 146.57 | 7.32 |
| 1 | Pooled, preparation disabled | 50.34 | 65.84 | 19.03 |
| 1 | Pooled, automatic preparation | 49.56 | 69.10 | 19.13 |
| 1 | Pooled, immediate preparation | 48.64 | 66.49 | 19.05 |
| 4 | Fresh connections, current preparation default | 173.44 | 202.78 | 19.35 |
| 4 | Pooled, preparation disabled | 71.12 | 87.37 | 48.73 |
| 4 | Pooled, automatic preparation | 68.30 | 88.42 | 50.73 |
| 4 | Pooled, immediate preparation | 68.68 | 84.80 | 48.44 |

With automatic preparation, pooled median latency was approximately 63% lower
at concurrency 1 and 61% lower at concurrency 4 than the contemporaneous fresh
connection baseline. Worker checkout averaged about 0.05 ms with pooling versus
25.82 ms and 33.02 ms for fresh connections. Admission and worker SQL time also
fell. This does not attribute all improvement exclusively to network handshakes:
session reuse can also avoid session-local initialization and repeated planning.
The experiment does not separately identify those SQL-time effects.

Automatic and immediate preparation produced prepared statements on reused
connections; disabling preparation did not. The inspection itself can add a
prepared statement when immediate preparation is enabled. Variations between
the three pooled configurations were small relative to the pooling effect and
do not establish a consistent benefit from forcing preparation immediately.

The new fresh-connection median was 133.42 ms at concurrency 1, whereas the earlier
separate local check measured 74.61 ms. These are separate sessions with a
different connection-lifetime adapter; the difference was not causally isolated.
Use only the balanced, contemporaneous baseline for this comparison. Do not
present these measurements as a direct 74.61-to-49.56 ms before/after claim.

## Method and scope

Four configurations, two concurrency levels, 100 measured operations per
configuration and 32 excluded warmups per configuration: 800 measured requests
plus 256 warmups. All paths use the same mock payment provider, shared HTTP
client, immutable admission implementation and worker lifecycle implementation.
Requests have unique resources and identities. Configuration order rotates
across ten blocks and reverses on alternating blocks.

SQLAlchemy QueuePool provides four connections per role with zero overflow;
the fresh reference uses NullPool. The admission and worker use separate
authenticated roles and separate pools. The prototype checks out an actual
native psycopg connection, executes the unchanged ExecutionWorker._execute,
and returns the lease with rollback-on-return. Rare lost-acknowledgement
observation/retry connections retain the original fresh, role-bound behavior.
The same lifetime adapter is used for all four configurations. Strict native
connection checks and the claim/fence/outcome commit sequence are preserved.

The fresh reference retains Psycopg's default prepare threshold of five;
pooled variants use disabled, automatic and immediate preparation. No new
dependency was installed. Protocol and script digests were recorded before
measurement. Timings use perf_counter_ns and nearest-rank percentiles.

Application HTTP/authentication, Celery/Redis queue scheduling, startup,
migrations, warmups and cleanup are excluded. This is neither full application
latency nor sustained production capacity. One hundred measurements per
configuration cannot establish a reliable tail-latency guarantee. No performance
SLO or sub-10-ms acceptance threshold was assumed.

## Safety and cleanup

Sixteen actual pool checks passed, covering both preparation-disabled and
immediate-preparation pooled modes:

- Duplicate delivery retains exactly one physical effect.
- Lost claim or fence acknowledgement prevents sending.
- Lost outcome acknowledgement observes committed state without resending.
- A closed/broken connection is discarded and the next operation succeeds.
- Transaction-local state is reset; authenticated database roles are preserved.
- A foreign process is refused before pool checkout.
- Non-idempotent email with a lost response escalates without a second effect.

All 1056 HTTP requests, including warmups, completed with exactly one provider
call and one effect. Every durable operation independently reads back as
SUCCEEDED. Each measured worker observes five SQL calls and three separate
commit acknowledgements. Temporary schemas, roles and mock threads were removed.
All 504 artifacts in eleven earlier sealed packets remain unchanged.

## Before pre-run

The experiment supports implementing bounded connection reuse while retaining
automatic statement preparation. The prototype has not changed runtime behavior.
Enabling it in the application requires a reviewed implementation covering
connection ownership, role isolation, fork lifecycle, rollback/reset and broken
connections, followed by full regressions and an updated source freeze/review.
The existing frozen build still describes the original runtime. Do not start
qualification expecting the experimental pooled timings to apply to it.

Pre-run and big-run execution remain paused. The user must be informed before
pre-run begins; the existing explicit-go requirement for the final big run
remains in force.

Evidence: [protocol](protocol.json), [summary](summary.json),
[all samples](samples.jsonl), [blocks](blocks.json),
[independent provider ledger](provider-ledger.json),
[pool safety checks](pool-safety-checks.json),
[prepared-statement observations](prepared-statements.json),
[source script](experiment.py), [execution log](execution.log),
[cleanup](cleanup.json), [verification](verification.json) and
[artifact hashes](artifact-sha256.json).
