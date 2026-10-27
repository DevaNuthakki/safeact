# M5.9 F13 workflow development evidence, 2026-10-22

SafeAct arm only, uncommitted development source based on `3269385`. No commit,
Stripe/model call or frozen qualification run was performed. See the
[design and implementation](../workflow-qualification.md).

## Results

Eight cases (five F13 workflows and three standalone controls) completed in
100.2 seconds. All had zero harness errors, observed safety violations,
projection drift and incomplete quiescence. Useful completion was 5/5
achievable requested units: 2/2 workflows plus 3/3 controls. The three
unresolvable workflow variants are retained separately, not scored as useful
completion.

| F13 case | Injected behavior | Forward effects | Inverse effects | Outcome |
|---|---|---:|---:|---|
| 0000 | Refund applies; first reply lost | 1 | 1 | Verified compensation through a matching lookup |
| 0001 | Refund applies; all replies lost and lookup remains hidden | 1 | 1 | Compensation unresolved; no restoration claim |
| 0002 | Refund rejected before execution | 1 | 0 | Compensation unresolved; original charge remains |
| 0003 | Order applies; reply lost and order remains unindexed | 2 | 0 | Workflow unresolved; no compensating refund |
| 0004 | No injected fault | 2 | 0 | Workflow completed |

Including controls: 10 forward effects and 2 inverse effects, with zero
forward duplicates, duplicate inverses, invalid inverses, orphan effects or
false claims. Separate identities and counts are retained for charge, order
and compensation. An inverse never erases the original charge history.

Direct coverage credited two applied-refund/lost-response interleavings,
one lookup-confirmed compensation, one unresolved applied refund, and one
rejected refund without restoration. Other critical interleavings remain
explicitly absent in [coverage.json](coverage.json); this is not full coverage.

## Reproduce and inspect

```sh
SAFEACT_RUN_QUALIFICATION=1 .venv/bin/python -m chaos.qualify \
  --corpus dev --families CONTROL,F13 --slots 1 --out /tmp/workflows-new
```

Run with the local PostgreSQL, Redis and Toxiproxy services available. Every
case owns a fresh disposable schema and worker process stack. The environment
and per-case load samples are retained; concurrent regression activity means
these timings are not load-test or slot-calibration results.

- [raw.jsonl](raw.jsonl): admissions, all provider effects, HTTP and committed
  DB observations, child step bindings, process/broker traces and quiescence.
- [results.jsonl](results.jsonl): independent oracle verdicts and per-step counts.
- [summary.json](summary.json), [report.md](report.md): safety, useful completion,
  distinct outcomes, forward/inverse counts and mock provider cost.
- [environment.json](environment.json): run environment and scope.
- [source-sha256.json](source-sha256.json): uncommitted SUT/harness sources and
  the new workflow tests. The frozen corpus and scoring are not yet defined.
- [source.tar.gz](source.tar.gz): the actual source files matching those
  digests, retained so this development version remains inspectable without
  creating a commit. Credential files and configuration secrets are excluded.
- [artifact-sha256.json](artifact-sha256.json): hashes of the retained evidence,
  source archive and regression logs.

Every saved result was recalculated using `Scenario.from_json`,
`oracle.evaluate` and `coverage.observed`. After JSON normalization all eight
rows matched exactly, as did the recomputed aggregates, rendered report,
coverage counts and development exit status. All quiescence flags passed,
and the evaluation secret scanner reported no findings.

## Regression checks and remaining work

Nineteen new Phase A tests exercise false restoration, wrong amount/target,
shared identities, invalid contract/link, duplicate inverse, duplicate charge,
missing order effect, orphan effects, missing DB observations, cleanup-only
effects, ambiguous/unfinished HTTP exchanges and observer failure propagation.
The PostgreSQL observer test proves that another connection cannot see an
uncommitted claim, then observes it after commit.

Final checks: **1,313 distinct Python tests** (744 Phase A, 538 PostgreSQL/broker,
21 isolated PostgreSQL, 8 API E2E, 2 scripted-agent) and **35 console tests**
(25 Vitest, 10 Playwright) passed, plus typecheck/lint/build. Final database
discovery included the isolated-Redis opt-in and skipped no tests.

The first full database suite encountered a five-second statement timeout in
the existing concurrent-login test, whose assertion saw 7 rather than 8 rows.
It passed on an independent targeted rerun, followed by a complete database
rerun after the other suites finished. Timeouts and assertions were unchanged.
Both full outcomes are retained; resource contention is suspected, not proven.

| Database check | Passed | Failed | Elapsed |
|---|---:|---:|---:|
| [First full run](regressions/integration-first.log) | 537 | 1 | 837.0 s |
| [Targeted concurrent-login rerun](regressions/concurrent-login-rerun.log) | 1 | 0 | 5.2 s |
| [Final full run](regressions/integration-final.log) | 538 | 0 | 687.2 s |

Other captured logs and command summaries are in
[regression-checks.json](regression-checks.json). All runtime evidence and
the archived source files passed the existing secret scanner.

Compatibility checks of existing F06/F07/F09 cases also passed: two achievable
completions and one expected escalation after key expiry. Their local raw
output remains at `/tmp/safeact-m59-workflow-provider-regression`.

F14/F15 drivers, remaining interleavings, baseline evidence adapters and the
achievable-completion audit remain open. The original F16 retry-storm and
outage completion failures remain unchanged evidence. STOP 2 is still required
before running the full frozen corpus. See the
[next-driver plan](../review-agent-qualification-plan.md); standalone F13 does
not qualify the held-out workflow combinations.
