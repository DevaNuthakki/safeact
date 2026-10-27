# B3 workflow and review development evidence, 2026-10-23

Uncommitted implementation over base `3269385`. B3 now has its own fixed
workflow and authenticated human-review runtime. It remains unregistered as a
comparison arm. Frozen qualification and the release load benchmark have not
run. No real Stripe call, paid model call or commit was performed.

## Implemented behavior

The [plan](plans/b3-workflow-review-plan.md) precedes the implementation.
Workflow creation commits the complete original request, first charge, step
binding, audit and outbox together. Replays compare every original field,
including customer, fee, requester and inverse contract. Six concurrent
creations return one workflow; six advancers admit one next step.

Advancement serializes through the original charge job, then rereads bindings
and locks child jobs in fixed order. Workflow and step records stay insert-only.
An actual confirmed charge permits the order. A confirmed order failure with
non-application proof for every attempt permits a separately identified refund
of that actual charge reference, exact original amount and account. Uncertain
charge/order never permits the next unsafe step. Unverified or rejected refunds
retain unresolved compensation and the original forward history. A separate
advancer process uses the same transactions.

B3's local review API uses pgcrypto bcrypt-12 human passwords and hash-only,
expiring bearer sessions. Tenant and roles come from live server-side records.
The harness administrator provisions humans and policies; the application
cannot provision either. Validation and errors exclude credentials, and
statement, duration, sampling and parameter logging are suppressed before
password handling. The reviewer must differ from both requester and proposer.

A proposal binds action, displayed state/version, immutable intent, ordered
attempts, actual observations, policy revision and expiry. An operation row
lock serializes decisions. One reviewer/key commits; another key refuses; the
winning click replays even after execution progresses. Changed evidence, policy
or expiry causes a committed stale refusal. Rejected proposals mint no permission.

PROTECTED_RETRY creates one authorization for the next attempt. Claim and fence
recheck its evidence/policy/intent binding and expiry alongside the normal
contract, budget and key-window checks. The attempt fence consumes it in the
same transaction. Approval never invents provider proof. REVERIFY authorizes
one lookup reservation, including past the ordinary interval/horizon, and
cannot dispatch an action. A late actual response can wake reconciliation for
ready work even while no retry message is delivered.

This is local baseline infrastructure. Product UI/authentication and frozen
SafeAct SQL are unchanged. There is no guarded-agent surface: F15 remains N/A
for B3. Its review surface provides REVERIFY and PROTECTED_RETRY; it does not
provide caller attestation. Baseline normalization, cross-arm drivers and
registration remain necessary before making a comparison claim.

## Twelve retained development cases

The [summary](raw/summary.json) records **12/12 checks passed**, eighteen effects,
twenty-seven dispatch calls, twenty lookups, no harness errors and complete
cleanup. These selected cases are not a frozen-corpus denominator. They reuse
real integration checks and are not counted again as distinct Python tests.

| Case | Independently observed outcome |
|---|---|
| Normal replacement | Confirmed charge and order; no inverse |
| Permanently rejected order | Separate refund targets the actual original charge and amount; verified compensation |
| Lost refund reply | Matching lookup verifies one inverse effect without another dispatch |
| Hidden refund replies/index | Three protected refund calls produce one effect, but compensation stays unresolved |
| Permanently rejected refund | Charge remains visible; no inverse effect; compensation stays unresolved |
| Uncertain order | Two forward effects, no refund; unresolved order |
| Uncertain charge then actual positive lookup | No order before charge confirmation; eventual completed replacement |
| Concurrent human reviewers | Two actual HTTP requests blocked through a PostgreSQL wait graph; one decision, losing refusal, winning-key replay and one approved retry |
| New real evidence after display | Actual matching lookup commits success; old approval refuses stale and is audited |
| Reverify after actual horizon | Twenty-second horizon passes after escalation; each approval permits exactly one extra lookup, with no dispatch; fresh evidence finally resolves the effect |
| Late lookup after retry approval | New matching evidence resolves ready work without publishing or executing a second attempt |
| Payments 429 and approval | Actual Retry-After remains a dispatch floor; no unnecessary lookup; one successful approved retry |

The independent effect split is **fifteen forward effects and three inverses**.
Three workflow cases deliberately remain unresolved. Their actual effects
remain counted; uncertainty is not relabeled as successful business completion.

[devcheck.py](devcheck.py) retains committed snapshots from a separate
application-role connection, actual provider ledger events, HTTP exchanges,
human request intervals, public actor/role fields, decision/audit rows and
private process commit traces. It excludes passwords, session tokens, password
hashes and private process configuration. The harness explicitly checks its
own credential values before retaining output. Providers and owned processes
are stopped and disposable schemas/private files removed after observation.
The final provider ledger equals its pre-cleanup copy in every selected case.

[replay.py](replay.py) independently joins every dispatch to a committed fence,
every effect to its immutable operation, every positive reference to an actual
effect, and each inverse to the original charge, amount, account and failure
preconditions. It also checks actual reviewer overlap, the winning decision,
stale refusal, one-use authorizations, the expired-horizon lookup and Retry-After
timing. It reproduces [raw/replay.json](raw/replay.json). It is a development
verifier, not the main qualification oracle.

[counterexamples.py](counterexamples.py) rejects all fifteen
[synthetic data mutations](counterexamples.json), including wrong inverse
amount/link, duplicate inverse, false restoration, orphan/cleanup-only effect,
self-approval, missing reviewer role, invented overlap, duplicate decision,
missing fence, stale approval without prior evidence, reused authorization,
early retry and missing late-evidence recovery. These mutations are not provider
executions or qualification coverage.

## Reproduce

From the repository root with the named source overlay and local PostgreSQL:

```sh
PYTHONPATH=. SAFEACT_RUN_BASELINE_DEV_CHECK=1 \
  SAFEACT_RUN_DB_INTEGRATION=1 SAFEACT_RUN_DISPOSABLE_DB_TESTS=1 \
  .venv/bin/python docs/phase-e/dev-baseline-workflow-review-20261023/devcheck.py \
  --out /tmp/b3-workflow-review-new
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \
  docs/phase-e/dev-baseline-workflow-review-20261023/replay.py \
  /tmp/b3-workflow-review-new --out /tmp/b3-workflow-review-new/replay.json
```

The output directory must be new. Replay of the packet's `raw/` needs no
services. The synthetic verifier check additionally accepts `--out` for a new
JSON report. [regression-runner.py](regression-runner.py) records the full
validation commands; its fixed `/tmp` log names preserve this execution record.

## Regression verification

| Final check | Tests passed |
|---|---:|
| Phase A discovery | 817 |
| Complete PostgreSQL/broker discovery, including isolated Redis | 624 |
| Isolated PostgreSQL security | 21 |
| API journeys | 9 |
| Scripted agent harness | 2 |
| Console unit | 37 |
| Browser journeys | 14 |

That is **1,473 distinct Python tests and 51 console tests**. The complete
database discovery passed in 963.410 seconds with no skips. Its total includes
all 76 B3 admission/execution/workflow/review tests: eighteen workflow and
nineteen review integration tests are new in this slice. Six new input-boundary
tests are already included in Phase A. Targeted reruns, selected development
cases and synthetic verifier mutations are not counted again. Typecheck,
lint, build and diff checks also passed.

[regression-checks.json](regression-checks.json) records commands, opt-ins,
exit codes, durations, original log hashes and shared-log redaction counts.
[regressions/](regressions/) retains successful runs and original failures.
Shared log copies redact free-form 64-hex strings; named content digests remain
intact in structured raw observations. This full regression includes existing
bounded stress cases; the release load benchmark remains outstanding.

## Retained original failures

- The first workflow suite had sixteen privilege errors from row-locking
  insert-only workflow records, plus a rollback test listening on the observer
  engine. Coordination moved to the original mutable charge job; the listener
  now observes the writer. Grants were not broadened.
- The next iteration exposed an ambiguous SQLAlchemy join because a step has
  both operation and compensation foreign keys. The join now names the original
  operation explicitly. Human tests also found that Python bcrypt was not an
  installed/pinned dependency; the implementation uses existing pgcrypto instead.
- The corrected thirty-three-test workflow/review run passed. A later 75-test
  focused run retained one erroneous test assumption: CRM's 429 is ambiguous,
  so a protected retry was correctly refused. Payments now exercises the actual
  authoritative rejection/Retry-After path. A separate CRM refusal test passed.
  Neither connector contract nor a safety/timing assertion was loosened.
- [diagnostics/replay-initial.py](diagnostics/replay-initial.py) expected API
  cleanup exit zero. The pinned Uvicorn `capture_signals` drains then re-raises
  the fixture's owned-process SIGTERM, producing `-15`. The corrected verifier
  requires that exact exit for API cases and no process for other selected
  cases. Its original assertion failure is retained separately from SUT results.

## Source and remaining work

The [source manifest](source-sha256.json) and [archive](source.tar.gz) retain
410 runtime/test files over the named base. Four unchanged base files containing
scanner-shaped fixtures are referenced by exact hashes rather than copied.
Credentials/configuration files are excluded. Source consistency, retained
artifact hashes, documentation links, secret scans and unchanged HEAD/staging
were checked before sealing. [artifact-sha256.json](artifact-sha256.json) seals
the retained packet. Earlier packets remain unchanged; their 136
artifact hashes are recorded in [historical-packets.json](historical-packets.json).

Next: baseline evidence adapters and F13/F14 drivers, B0–B2, achievable-completion
and held-out construction audits, then corpus/scoring freeze for STOP 2.
The earlier F16 storm/outage shortfalls remain evidence. No cross-arm superiority
or full coverage is claimed. The author must explicitly authorize the final run
after a prominent **FINAL BIG QUALIFICATION RUN** announcement. That run has
not started.
