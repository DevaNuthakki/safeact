# F14/F15 development evidence, reviewed 2026-10-23

Uncommitted SafeAct development source on base `3269385`. No frozen
qualification, real Stripe call, paid model call or commit was performed.
[Driver design](../review-agent-qualification-plan.md).

Nine cases (three controls, three F14 and three F15) completed with **9/9
achievable requested operations**, nine effects, 55 provider calls, zero
harness errors, observed safety violations, projection drift or incomplete
quiescence. Every final outcome was confirmed success. F14 includes scripted,
authenticated human participants in its achievable denominator; F15 measures
deterministic boundary enforcement and provider recovery, not model ability.

| Case | Directly observed behavior |
|---|---|
| F14 0000 | Two distinct reviewers blocked on the same operation through a real PostgreSQL wait graph; one approval, one already-decided refusal, winning-key replay |
| F14 0001 | Matching real provider lookup commits new evidence before an old displayed proposal is refused and audited |
| F14 0002 | Actual retryable pre-execution rejection, explicit reviewed q-v2 contract, concurrent version-bound human approval, one successful retry |
| F15 0000–0002 | Four concurrent submissions across two scoped credentials for the same principal collapse to one intent; exact replays retain identity |
| F15 0000–0002 | Changed intent/payload, caller-supplied identity/key fields, revoked credential and another tenant's access are refused with independent before/after snapshots |
| F15 0000–0002 | Agent proposes REVERIFY but cannot approve through MCP or either human API credential form; no decision/evidence/dispatch is induced |

F14 0002 made two provider dispatches: one rejection and one applied effect.
Every other case made one dispatch and one effect. All five ambiguous CRM
cases completed through actual matching provider lookup after temporary index
lag was released. No human restoration reference was fabricated. Six new
critical boundary interleavings were observed; [coverage.json](coverage.json)
retains every other unobserved cell. This is not full qualification or a load
benchmark.

## Reproduce and inspect

```sh
SAFEACT_RUN_QUALIFICATION=1 .venv/bin/python -m chaos.qualify \
  --corpus dev --families CONTROL,F14,F15 --slots 1 --out /tmp/boundaries-new
```

Requires local PostgreSQL, Redis and Toxiproxy. Every case owns its disposable
schema and process stack. API/MCP startup and authentication finish before
the timed observation window. MCP admission intervals surround actual tool
calls after initialization. The review overlap is proved through independent
`pg_blocking_pids` observations, not inferred from two response codes.

The saved [raw.jsonl](raw.jsonl) was independently re-evaluated with the typed
scenario, oracle and coverage functions. All nine [result rows](results.jsonl),
[summary](summary.json), [rendered report](report.md), coverage counts and exit
status matched exactly. The [environment](environment.json) retains machine
load and scope. Credentials are absent; raw evidence retains actor/key ids,
public bindings, committed audit records and request intervals.

## Retained diagnostic failures

[diagnostics/](diagnostics/) preserves the original intermediate results; its
source was changing during development and is not the final qualification
snapshot. Do not combine those runs into the final nine-case denominator.

- The sandbox-only launch could not bind local sockets. An approved local
  rerun used the same local mock services; it was not a final-corpus run.
- The first lock observer checked only direct blockers. PostgreSQL may queue
  the second waiter behind the first; the old observer unnecessarily held
  both requests until the unchanged five-second API timeout. The final driver
  follows the transitive blocking graph and releases before that timeout.
- Positive reveal fixtures initially used permanent `drop_from_index`, which
  normal reindex correctly preserves. The final fixtures use temporary lag.
  The failed recovery expectation was a fixture error, not proof that an
  uncertain action should have been repeated.
- Those driver errors exposed missing timed views on the runner's exception
  path. Intermediate summaries consequently included unaccounted operations.
  The runner now preserves admitted-operation views even when a driver fails;
  it still fails the run for that driver error. Original summaries remain intact.
- An intermediate six-case corrected run passed; final verification added the
  three controls, actual MCP tool-call interval rendezvous and stricter coverage
  gates. Intermediate results do not replace final source/evidence.

## Regression verification

The original full database run and the unchanged isolated outage rerun are
retained in [regressions/](regressions/).
The first full run completed 547 tests in 1616.941 seconds with one existing
outage polling-count assertion failure: 10 passes versus >30. Its unchanged
isolated rerun passed in 5.192 seconds. Resource scheduling is a possible,
unproven explanation; no timeout or assertion was loosened. The quiet full
rerun passed all **547 tests in 702.147 seconds**, without skips.

| Final check | Tests passed |
|---|---:|
| Phase A discovery | 795 |
| Full PostgreSQL/broker discovery, including isolated Redis | 547 |
| New B3 admission tests, separately after that discovery | 9 |
| Isolated PostgreSQL security | 21 |
| API journeys | 9 |
| Scripted agent harness | 2 |
| Console unit | 37 |
| Browser journeys | 14 |

That is **1,383 distinct Python tests and 51 console tests**. Targeted reruns,
20 B3 unit tests already in Phase A, and the nine development cases are not
counted again. Typecheck, lint, build and diff checks passed. Commands, original
log hashes and copied-log redaction counts are in [regression-checks.json](regression-checks.json).
Shared log copies redact free-form 64-hex strings; structured raw evidence keeps
named content digests intact. Original logs remain unchanged in `/tmp`.

[qualification-source-sha256.json](qualification-source-sha256.json) and its
[archive](qualification-source.tar.gz) retain the 389-file runtime/test overlay
used for the nine-case check. Every original entry remained unchanged through
final verification. The final [source manifest](source-sha256.json) and
[archive](source.tar.gz) add five separately tested B3 foundation files, giving
394 files. Restore over the named base checkout; four unchanged base files with
scanner-shaped fixtures are referenced by exact base hashes instead of copied.
No credentials/configuration secret files are included. Source and artifact
digests, archive entries and the existing secret scan were verified; retained
artifact hashes are in [artifact-sha256.json](artifact-sha256.json).

The B3 foundation's initial unit expectations incorrectly assumed more attempts
than the real CRM/email profiles allow. Those failed and corrected logs are
retained. The final tests respect the shared contract budgets. The foundation
has 20 passing offline and nine own PostgreSQL tests, including independent
transaction visibility, six concurrent identical callers, changed-payload races,
atomic rollback and replay after a lost commit acknowledgement. It is not a
runnable/qualified baseline arm.

## Remaining work

B0–B3 are not runnable/qualified comparison arms yet. The [B3 design](../b3-design.md)
precedes its initial independent recovery/admission implementation. Baseline
workers, fencing, reconciliation, workflows, human review and evidence adapters
remain, followed by the achievable-completion/held-out-construction audits and
corpus/scoring freeze. The original F16 storm/outage shortfalls remain evidence.
STOP 2 and the author's explicit go after a prominent **FINAL BIG QUALIFICATION
RUN** announcement are required before that run starts.
