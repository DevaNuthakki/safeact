# Local recovery and latency checks — 2026-10-24

The user requested results for two concerns and explicitly prohibited pre-run
and big-run execution. These checks used existing recovery tests, disposable
PostgreSQL schemas and loopback mock HTTP providers. No frozen scenarios,
smoke sample, calibration, qualification, real Stripe or paid service ran.
Product code and frozen inputs are unchanged. Nothing was staged or committed.

## 1. Provider outcomes that cannot be verified

**The implemented conservative safeguard passes the targeted checks.** All 78
tests passed: 38 policy/contract/metric tests and 40 integration tests, zero skips.
The integration suite includes the non-idempotent email provider applying an
effect then losing its response: SafeAct escalates, a second reconciliation
skips the operation, and the independent provider ledger still has exactly one
call and one effect. Eventual empty lookup does not authorize retry; native-key
expiry and insufficient evidence block unsafe retry. Missing or foreign
contracts/evidence are refused. Lost claim/fence acknowledgement blocks sending;
lost outcome acknowledgement re-observes durable state rather than repeating
the effect. Real socket tests cover a lost reply and a later effect after an
empty lookup.

This is safe handling of uncertainty, not proof that every provider can be
verified or every operation can complete. The provider must supply the relevant
guarantee. Full frozen qualification is still pending.

See [test-checks.json](test-checks.json),
[policy results](policy-and-contracts.log) and
[integration results](recovery-integration.log).

## 2. Latency added by durable execution

**The measured local overhead is material; this concern is not closed.**
Each configuration contains 100 measured requests, plus five excluded warmups.
Direct HTTP and SafeAct use the same loopback payment mock, amount, reusable
HTTP client and unique operation identities. Ten balanced blocks alternate path
order. SafeAct runs real admission with NullPool and the unchanged synchronous
ExecutionWorker. Every admitted operation commits SUCCEEDED, with one physical
provider call and one effect. Setup, migrations, warmup and cleanup are excluded.

| Concurrent requests | Path | Median ms | p95 ms | p99 ms | Completed requests/s |
|---:|---|---:|---:|---:|---:|
| 1 | Direct mock HTTP | 1.98 | 2.54 | 2.97 | 478.53 |
| 1 | SafeAct admission + worker | 74.61 | 87.39 | 90.47 | 13.15 |
| 4 | Direct mock HTTP | 6.89 | 10.02 | 12.64 | 499.34 |
| 4 | SafeAct admission + worker | 98.81 | 155.35 | 223.82 | 31.46 |

At concurrency 1, SafeAct averages 75.92 ms overall:
29.09 ms admission,
13.87 ms worker connection,
25.04 ms worker SQL calls,
5.00 ms across the three worker commits,
and 2.07 ms provider HTTP.
The rest is Python/connector processing and timing overhead. Admission already
includes its database work, connections and commit. Thus these figures do not
double-count the worker SQL or its three commits. Five protected SQL calls and
three commit acknowledgements were observed in each successful worker request.
The total measured non-provider overhead averages
73.85 ms at concurrency 1 and
103.00 ms at concurrency 4.

Direct HTTP has no durable admission, recovery, fencing or audit guarantees; it
is a lower-bound reference, not an equivalent B0/B1/B2/B3 comparison. These
measurements exclude application HTTP/authentication and Redis/Celery/relay
scheduling, so they are not full application latency or production capacity.
The database is local to the existing development setup and the fixture opens
fresh database connections; do not generalize its cost to a pooled deployment.
At 100 observations per configuration, p99 is a descriptive sample statistic,
not a reliable tail-latency claim. No performance SLO was supplied, so no
latency threshold is labeled passing.

Connection reuse deserves investigation: both admission and the worker establish
fresh connections. The SQL calls also have measurable cost. Any later
optimization should be evaluated against a stated latency target and preserve
the acknowledged dispatch fence before provider transmission. No optimization
or change to those safeguards was attempted here.

See [protocol](benchmark-protocol.json), [summary](latency-summary.json),
[all samples](latency-samples.jsonl), [timed blocks](latency-blocks.json) and the
independent [provider ledger](provider-ledger.json). The scripts
[targeted-tests.py](targeted-tests.py) and [latency-check.py](latency-check.py)
record the exact methods used.

## Scope and evidence checks

All 400 measured requests and 20 warmups completed with zero errors and zero
duplicate effects. Temporary schemas, roles and provider threads were removed.
Frozen input digests still verify and the frozen execution ledger remains
absent. All 487 artifacts in ten earlier sealed packets remain unchanged.
Evidence is scanned for secret-shaped values and sealed by
[artifact-sha256.json](artifact-sha256.json).

The detailed [verification record](verification.json) retains these assertions.
Pre-run and big-run execution remain paused at the user's instruction.
