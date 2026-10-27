# B3 process observation development evidence, 2026-10-23

**FINAL BIG QUALIFICATION RUN: NOT STARTED. No commits.**

The [implementation plan](../b3-process-observation-plan.md) precedes this
slice. B3 now emits private HTTP, execution and broker observations from its
own processes. HTTP clients constructed after fork record actual exchanges;
runtime/workflow hooks retain actual transaction ids and native exception
classes; Celery publisher, receipt, start and return events bind four durable
transport identities to real worker process ids. The application still uses
its own ordinary transactions and the existing connector contracts. No SafeAct
SQL, migration, production grant, business transition or recovery policy changed.
HTTP traces retain exchange identity, provider, method/path and correlation
digests; payload digests remain in independent provider-call observations.

The trace writer requires a B3 disposable schema and private owned directory.
Files are append-locked, bounded, private, regular and single-linked. Symlinks,
hardlinks, FIFOs, public files, duplicate/partial JSON, unknown fields, malformed
ids and mismatched process filenames fail. Headers, payloads, credentials,
task bodies and exception messages cannot enter the protocols. Observer errors
emit fixed harness markers while preserving the underlying result. The future
arm must fail integrity on `BASELINE_TRACE_ERROR` or `SAFEACT_HTTP_TRACE_ERROR`.
SafeAct's trace schema guard remains unchanged.

Publisher observations are scoped to the exact app, task, queue and selected
task id through a per-call context. Worker signals check the app/task/queue.
Closing an app disconnects its observers. Native B3 result names remain intact;
no claimed success is manufactured from a task result. The common lost-ack
coverage check now accepts B3's actual `LostAcknowledgement` exception, still
requiring matching independently committed audit xid, hook stage, process and
before/return/error order. The original `ConnectionError` path is unchanged.

## Retained local checks

[Seven development cases](raw/summary.json) passed with **nine HTTP dispatches,
two HTTP lookups and nine effects**, complete schema/file/process cleanup and
no trace error markers. They reuse integration tests and do not add a second
test denominator. These selected cases do not register B3 as an arm, establish
frozen fault-family coverage, compare performance or replace final qualification.

| Case | Observations checked |
|---|---|
| All three connectors | Persisted dispatch fence/audit before HTTP, matching independent provider exchange |
| Lost outcome acknowledgement | Same committed xid and actual hook error; replay sends no second request |
| Lost fence acknowledgement | Persisted dispatch/attempt, no HTTP or provider call |
| SIGKILL after HTTP effect | Actual owned process exit `-9`, matching hook/HTTP/effect, lookup recovery without resend |
| Real Redis/prefork delivery | Publication/receipt/child execution/return share task/outbox/delivery ids; worker HTTP and committed delivery match |
| Separate reconciler process | Its actual GET and committed lookup result resolve the existing dispatch |
| Workflow transaction ids | Native workflow identity and commit ids match independently read advancement audit |

The [offline replay](replay.py) validates these joins from saved data. The
[counterexample checker](counterexamples.py) rejects 28 saved-data mutations,
including wrong xid/process/order, omitted HTTP/effect/kill evidence, invented
prefork identity, missing committed delivery, altered workflow identity and
sensitive trace fields. These mutations are not new provider executions or
unittest cases. Earlier B3 adapter records and all seventeen prior SafeAct
workflow/boundary results also replay unchanged.

## Failures retained

The first new unit run had a duplicate keyword in a mutation fixture; it is
corrected without changing the assertion. Full discovery then found the factory
accessed `trace_dir` on a minimal configuration even when tracing was disabled;
the role check now precedes optional trace configuration access.

Concurrent initial `openat` creation intermittently returned `ENOENT` for the
same trace file, reproduced in both sandboxed and unrestricted 50-repeat checks.
The writer now creates exclusively, then opens an existing file when another
creator won. Existing-file validation, append locks and refusal behavior remain.
All fifty unchanged repetitions passed after that correction. This is not a
retry on a missing directory or a weakened concurrency assertion. The failed
focused/full runs and both original repeat checks are retained separately.

## Verification and source

**1,513 distinct Python tests passed:** 838 Phase A, 643 PostgreSQL/broker,
21 isolated security, nine API and two scripted-agent harness tests. The
integration run finished in 933.456 seconds without skips, including all seven
new process integration tests. Thirteen new offline trace tests are included
in Phase A. **51 console tests passed:** 37 Vitest and 14 Playwright, with
typecheck, lint, build and diff checks also passing. The fifty focused repeats,
seven retained executions and 28 saved-data mutations are not counted again.

The [regression record](regression-checks.json) retains exact commands, flags,
timings, log fingerprints and original diagnostics. The
[initial failed check record](regressions/initial-checks.json) and separate
failed/corrected logs preserve the fixture, factory and concurrent-create
failures. The final development log contains no trace-error markers.

The [source manifest](source-sha256.json) and [archive](source.tar.gz) capture
416 runtime/test files; four unchanged fixture files containing scanner-shaped
literals are referenced by exact base hashes instead of copied. The
[source delta](source-delta.json) adds three files and changes only
`chaos/baselines/processes.py` and `chaos/coverage.py`; 411 earlier source files
remain unchanged. [Historical verification](historical-packets.json) checks
all 251 artifacts in seven earlier packets without rewriting them.
[Historical replay](historical-replay.json) matches five prior B3 records and
seventeen SafeAct results. The new [replay result](replay.json) and
[counterexamples](counterexamples.json) are retained separately.

The [artifact manifest](artifact-sha256.json) fingerprints this packet. Current
source/archive, secret and documentation-link checks passed. HEAD and staging
remain unchanged. This is development evidence, not the frozen qualification
or release load benchmark.

Replay from the repository root without services:

```sh
PYTHONPATH=. .venv/bin/python docs/phase-e/dev-baseline-process-trace-20261023/replay.py docs/phase-e/dev-baseline-process-trace-20261023
PYTHONPATH=. .venv/bin/python docs/phase-e/dev-baseline-process-trace-20261023/counterexamples.py docs/phase-e/dev-baseline-process-trace-20261023
```

To reproduce the seven local cases, run [devcheck.py](devcheck.py) with
`PYTHONPATH=.`, a fresh `--out` directory and all four flags set to `1`:
`SAFEACT_RUN_BASELINE_DEV_CHECK`, `SAFEACT_RUN_DB_INTEGRATION`,
`SAFEACT_RUN_DISPOSABLE_DB_TESTS` and `SAFEACT_RUN_BROKER_TESTS`. This uses local
mock providers, disposable PostgreSQL and reserved Redis test DB 9. It refuses
that broker database if another client is using it. No real provider/model
call is part of the check.

Still required: B3 owned stack/quiescence, native-key profile registration
coordination, complete shared-runner fault drivers and F14 normalization;
B0–B2; construction/scoring/achievability audits; corpus freeze and STOP 2;
then explicit final-run authorization. No partial arm is registered and no
unsupported fault is silently relabeled N/A. Production execution remains denied.
