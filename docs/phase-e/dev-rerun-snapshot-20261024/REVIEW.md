# Development rerun after the snapshot fix (c55dbae)

Your go (2026-10-24): fix the snapshot race, apply it to every arm, re-pin, rerun the
affected development families and compare. The scoring code is shared by every family,
so all 78 development cases were rerun on all five arms. **No frozen smoke, calibration
or full run was started.** No real Stripe or paid model call was made.

| | |
|---|---|
| System under test | commit `c55dbae` (detached worktree, clean tree), manifest `sha256: 463a5cc633134764224c55c52bcdda9596be2cd6c026b39dbf4ba7f9beeef585` |
| Command | `SAFEACT_RUN_QUALIFICATION=1 caffeinate -i python -m chaos.qualify --corpus dev --arms safeact,b0,b1,b2,b3 --slots 1` |
| Cases | 78 cases (seed 7) x 5 arms = 390 rows; 0 harness errors; exit 0; 4,402 s |
| Machine | AC power at start and end (battery 87% -> 100%); caffeinate active; 1-min load 3.31 at start, 1.99 at end |
| Baseline | 390 rows: the `fb89604` rows for the other families plus the `a485860` F05/F06/F16 rows (`baseline sha256: 8919b15ea5f6618b62b27441f8f40007de59f5f9c303a84654b7c6380537dfc4`, rebuilt exactly from the two committed packets) |

## The settle rule chosen

The preferred rule, waiting until no operation can still change state, was measured
first and rejected. On the 304 executed rows of the previous runs, waiting out the
post-escalation lookup horizon (20 s for payments and CRM) adds 22% observation time.
For 14 of 39 rows with escalations, it would also need longer than the frozen
per-family `settle_seconds`. That is not "little time" under `q-v2`.

So every arm now scores the snapshot that passed the stability check (`chaos/runner.py`).
A row that never became stable is scored at the window close, and `scored_from` says
which. The runner still takes the window-close and final snapshots. Where either one
differs from the scored state, both states go on the row (`unstable_at_scoring`).
Such rows are counted per arm in the summary (`unstable_at_scoring_rows`, scoring
version 3) and shown as a report column. A snapshot that was not taken is never
counted as a difference.

## Comparison

390 pairs were compared on outcome fields; timing is never compared. **5 pairs differ.
None changes a safety count, integrity or a harness error.**

| Case (seed 7) | Arm | Before | Now | Cause |
|---|---|---|---|---|
| D-F16-0001 | B3 | 2 x escalated, 1 x pending | 3 x escalated_unresolved | the fix: the stable snapshot is scored |
| D-F16-0004 | B3 | pending | escalated_unresolved | the fix |
| D-F01-0001 | B0 | confirmed_success | pending | toxic window timing; effects unchanged |
| D-F01-0001 | B2 | pending | confirmed_success | the same; earlier runs had both classes |
| D-F16-0002 | **SafeAct** | 5/5, one effect each | op 0 escalated_unresolved, 0 effects; **4/5** | jitter, budget exhausted inside the outage (below) |

**Unstable at scoring: 1 row.** In D-F05-0002 B3, the operation was scored
ESCALATED_UNRESOLVED. At window close it was in a post-escalation reopen (VERIFYING),
and its final state is ESCALATED_UNRESOLVED again. This is exactly the race, now
recorded rather than scored. SafeAct, B0, B1 and B2 have 0 such rows.

**Scored at window close** (never stable): B0 20, B1 15, B2 15 rows with pending
operations the blind baselines leave unresolved, plus D-F13-0002 in SafeAct and B3. That
case was pending in every arm before as well.

### SafeAct D-F16-0002: safe, but a usefulness miss again

This is the case of the `30d7bd8` fix. The fix acted: both retries of operation 0 were
released as `RETRY_ELIGIBLE:provider_key_still_protected`. This time all three
dispatches landed inside the 5.99 s outage:

| Event | Offset from outage start |
|---|---:|
| attempt 1 dispatched | +0.13 s |
| attempt 2 dispatched | +2.45 s |
| attempt 3 dispatched | +5.60 s |
| outage ends | +6.00 s |
| first post-outage verification | +6.73 s |

After the third attempt the budget (`max_attempts` 3) is spent, so `can_retry` is
false. Negative coverage stays incomplete because an in-outage attempt is still
`MAY_HAVE_DISPATCHED`. The reconciler verified until the deadline, escalated
(`negative_evidence_coverage_incomplete`), and its reopens went back to escalation:
0 effects, no duplicate. A fourth dispatch would exceed the attempt budget that SAT13
enforces. In the `a485860` run, jitter put the third dispatch after the outage, and
it succeeded.

So the policy behaved as declared. But this is the case you reviewed, and calibration
creates the execution ledger, after which no re-pin is possible. **I therefore stopped
before calibration** so you can decide this first:

- **(a) Accept (recommended).** Record it as a retained usefulness result under the
  jitter disclosure: whether a payments outage can exhaust the budget depends on the
  run.
- **(b) Change the policy before the freeze.** For example, a provider-outage error
  could stop consuming an attempt. That loosens the budget semantics. It needs a
  design review, a re-pin and another rerun.

## Totals by arm

| Arm | Useful / achievable | Safety-failure rows | Duplicates | Unstable at scoring |
|---|---:|---:|---:|---:|
| SafeAct | 62/63 | 0 | 0 | 0 |
| B3 | 60/60 | 0 | 0 | 1 |
| B2 | 43/50 | 7 | 7 | 0 |
| B1 | 35/50 | 16 | 14 | 0 |
| B0 | 7/24 | 12 | 13 | 0 |

Coverage: SafeAct and B3 observed every applicable critical interleaving (none
unobserved). B0, B1 and B2 lack the same labels as before.

## Files

- [comparison.json](comparison.json): differences and per-family figures, before and
  after. Produced by `../dev-rerun-post-pooling-20261024/compare_rerun.py`.
- The run's own outputs: [results.jsonl](results.jsonl), [summary.json](summary.json),
  [coverage.json](coverage.json), [environment.json](environment.json) and
  [report.md](report.md).
- `raw.jsonl` (71 MB) and the log are in
  `~/safeact-evidence/docs/phase-e/dev-rerun-snapshot-20261024/`; their digests are in
  [outside-repo.json](outside-repo.json).
- [artifact-sha256.json](artifact-sha256.json) seals this packet.
