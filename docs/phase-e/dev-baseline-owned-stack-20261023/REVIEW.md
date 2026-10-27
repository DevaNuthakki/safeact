# B3 owned stack development evidence, 2026-10-23

**FINAL BIG QUALIFICATION RUN: NOT STARTED. No commits.**

The [plan](../b3-owned-stack-plan.md) precedes this implementation. B3 now has
a harness-owned disposable database, private configuration and queue, separate
worker/relay/reconciler/advancer/API process groups, and a registration controller.
The stack consumes the slot's broker and provider proxy ports; it never flushes
or owns those shared slot resources. Worker and advancer processes receive no
mock administration or independent provider-ledger access. Provider contracts,
attempt/investigation budgets and recovery decisions remain unchanged.

After a delivery reservation commits, the relay writes a fresh private request
with its actual PID and transaction id. It waits outside the database transaction.
The controller independently reads committed immutable jobs, validates their
bindings, registers native mock keys with the declared contracts and acknowledges
that exact request. The relay records completion before publishing any task.
Every replayed reservation and relay generation uses the same gate, including
workflow children created by the advancer. Original one-shot fault hooks run
first, so lost commit acknowledgements retain their original behavior.

Messages use bounded complete JSON, exact fields, canonical identities, private
owned directories and regular single-link files, nonblocking file locks and
atomic publication. Missing, stale, foreign, malformed or refused confirmations,
controller failure and timeouts never default to publication. Waiting can be
cancelled on shutdown while the accepted intent remains durably recorded.

All process generations remain owned through restarts. Quiescence stops and
reaps roots, verifies every group has disappeared, then stops the controller.
Provider admission closes before outstanding calls and held commits drain.
Final database/provider observations precede schema/role removal and private
configuration deletion. Unexpected exits and cleanup errors remain failures,
including an unrequested clean exit followed by a replacement.

## Selected development checks

The retained checks reuse integration tests; they add no second test denominator.
All [thirteen final-source checks](raw/summary.json) passed, including four
explicit negative integrity checks, with **eleven HTTP dispatches, ten effects,
twelve completed gates/publications and 34 owned process generations**. All
schemas, private files, controller threads and process groups were cleaned up.
They exercise registration blocking with independently acquired database row
locks, workflow charge/refund children and concurrent admission, lost publication
acknowledgements, controller and registration failure, changed immutable bindings,
shutdown while waiting, relay and prefork-worker replacement, late held effects,
review API login and admission rollback. A separate lifecycle case verifies that
a replacement cannot erase an unrequested clean relay exit.

Expected controller/binding/lifecycle failures are explicitly labeled negative
checks. Their integrity records remain in the raw data. In refusal cases,
SIGTERM can reach a relay already exiting after `BASELINE_PROCESS_ERROR`; the
resulting unexpected exit remains recorded rather than counted as clean shutdown.
These cases establish refusal and retained accounting, not successful execution.

These tests use real local HTTP mocks, PostgreSQL and reserved Redis test DB 9.
Their slot-shaped fixture supplies the three direct mock ports, excluding the
admin endpoint. They test construction and process behavior; B3 traffic through
real Toxiproxy faults still requires the common-runner adapter. API health and
live process IDs establish startup only. Future arm warm-up must finish before
timing scenarios. Raw provider holds must be passed to `drain`/`close` or owned
through `Stack.hold_next` so late commits remain part of the drain barrier.

The [offline replay](replay.py) joins exact gate identities and intervals,
independent immutable database bindings, real commit-return observations,
actual broker publications and committed delivery identities, all process
generations, effects and cleanup. It does not claim that an acknowledgement
alone proves a database commit. The [counterexamples](counterexamples.py)
reject corrupted saved data separately from new executions. The
[historical replay](historical_replay.py) rechecks earlier process observations,
native B3 oracle cases and SafeAct results without changing the earlier packets.

Registration request-to-finished intervals are retained as development overhead.
The [twelve intervals](gate-overhead.json) range from 44.706 to 90.320 ms, with
a median of 48.086 ms in these selected checks.
Some checks deliberately pause registration. They are not a representative
latency benchmark, and no cross-arm timing comparison is claimed. The future
runner must review/calibrate this harness overhead across arms before comparison.

## Failures and verification

The first integration attempt supplied the fixture's admin endpoint as a fourth
provider. Configuration correctly refused it before any execution; the fixture
now supplies only the three provider ports. Cleanup records the stack before
startup and attempts final close even if observation or drain fails.

Review then found that an unrequested zero exit could disappear behind a
replacement. The stack now retains that failure, and the added actual-process
test passes. The offline checker initially expected every failed relay to exit
with code 1; actual refusal data retained `BASELINE_PROCESS_ERROR` and a racing
SIGTERM exit. Replay now requires the explicit retained failure records. Its
mutation driver also needed to catch the registration protocol's own error type.
Original diagnostics are retained; no runtime assertion was weakened.

**1,537 distinct Python tests passed:** 849 Phase A, 656 PostgreSQL/broker,
21 isolated security, nine API and two scripted-agent tests. Integration finished
in 995.262 seconds with no skips, including all 108 B3 integration tests.
**51 console tests passed:** 37 Vitest and 14 Playwright. Type checking, lint,
build and diff checks passed. Checks completed before the clean-exit fix were
repeated against the final source; both execution records remain. The
[test inventory](test-inventory.json) verifies unique IDs. Focused reruns,
thirteen retained cases and 33 saved-data mutations are not counted again.

The [regression record](regression-checks.json) retains exact commands, flags,
timings, original log fingerprints and separate failed/corrected diagnostics.
The [initial twelve-case data](initial-development.tar.gz) predates the final
lifecycle fix; the `raw/` directory contains the thirteen final-source cases.
[Replay](replay.json) validates those cases and
[counterexamples](counterexamples.json) records all 33 refusals. Earlier
[seven process cases, five B3 oracle cases and seventeen SafeAct results](historical-replay.json)
still replay unchanged.

The [source manifest](source-sha256.json) and [archive](source.tar.gz) capture
420 runtime/test files. Four unchanged scanner-shaped fixture files remain
exact base-only references. The [delta](source-delta.json) adds four files,
changes only `environment.py` and `processes.py` among existing source files,
and preserves 414 earlier files. [Historical verification](historical-packets.json)
preserves all 300 artifacts in eight earlier packets. The
[artifact manifest](artifact-sha256.json) fingerprints this packet. Current
source/archive equality, secret and documentation-link checks passed; HEAD and
staging remain unchanged. This is development evidence, not frozen qualification
or the release load benchmark.

## Reproduction and remaining work

From the repository root, replay without services:

```sh
PYTHONPATH=. .venv/bin/python docs/phase-e/dev-baseline-owned-stack-20261023/replay.py docs/phase-e/dev-baseline-owned-stack-20261023
PYTHONPATH=. .venv/bin/python docs/phase-e/dev-baseline-owned-stack-20261023/counterexamples.py docs/phase-e/dev-baseline-owned-stack-20261023
PYTHONPATH=. .venv/bin/python docs/phase-e/dev-baseline-owned-stack-20261023/historical_replay.py docs/phase-e/dev-baseline-owned-stack-20261023
```

Run [devcheck.py](devcheck.py) with `PYTHONPATH=.`, a fresh `--out` directory and
all four flags set to `1`: `SAFEACT_RUN_BASELINE_DEV_CHECK`,
`SAFEACT_RUN_DB_INTEGRATION`, `SAFEACT_RUN_DISPOSABLE_DB_TESTS`,
`SAFEACT_RUN_BROKER_TESTS`. This uses disposable local services and mock providers.

Still required: complete shared-runner B3 fault drivers and F13/F14 normalization;
B0–B2; construction/scoring/achievability audits; corpus freeze and STOP 2; explicit
final-run authorization; qualification/load, Compose packaging and demo evidence.
No partial comparison arm is registered or unsupported family relabeled N/A.
This slice makes no real Stripe or paid model calls. Production remains denied.
