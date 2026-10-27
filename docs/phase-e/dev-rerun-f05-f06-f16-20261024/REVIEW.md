# F05/F06/F16 development rerun after the STOP 2 changes (a485860)

You approved four changes on 2026-10-24: the native-key retry fix (`30d7bd8`), email
cells (`5a23e89`), the per-intent oracle tests (`a414398`) and the run-environment gate
(`a485860`). This rerun covers the development families they affect. **No frozen
smoke, calibration or full run was started.** No real Stripe or paid model call was
made.

| | |
|---|---|
| System under test | commit `a485860` (detached worktree, clean tree) |
| Command | `SAFEACT_RUN_QUALIFICATION=1 caffeinate -i python -m chaos.qualify --corpus dev --families F05,F06,F16 --arms safeact,b0,b1,b2,b3 --slots 1` |
| Cases | 17 cases (F05 6, F06 5, F16 6; seed 7) x 5 arms = 85 rows; 0 harness errors; exit 0; 1,762 s |
| Machine | AC power at start and end (battery charging 13% -> 50%); caffeinate held for the whole run; 1-min load 2.95 at start, 1.48 at end |
| Baseline | the 70 F05/F06/F16 rows of the post-pooling rerun on `fb89604` ([dev-rerun-post-pooling-20261024](../dev-rerun-post-pooling-20261024/REVIEW.md)) |

Development mode does not start caffeinate itself, so the command was wrapped in
`caffeinate -i`. Frozen runs now start their own (`a485860`).

## Comparison

70 pairs were compared on the same outcome fields as before; timing is never compared.
**4 pairs differ. None changes a safety count, integrity or a harness error.** 15 rows
are new: the three email cases x 5 arms.

| Case (seed 7) | Arm | Pre (fb89604) | Post (a485860) | Cause |
|---|---|---|---|---|
| D-F16-0002 | SafeAct | op 0 escalated_unresolved, 0 effects; 4/5 useful | op 0 confirmed_success, 1 effect; **5/5** | the fix: same-key retry with partial negative proof |
| D-F16-0001 | B3 | 3 x escalated_unresolved | 2 x escalated, 1 x pending | harness snapshot race (below); final state escalated |
| D-F16-0004 | B3 | escalated_unresolved | pending | the same race; final state escalated |
| D-F16-0003 | B3 | label provider_outage_spanned_deadline | no label | timing variance; also absent in 2 of 3 pre-pooling B3 runs |

**The fix, observed.** All five D-F16-0002 operations succeeded with exactly one effect.
The other SafeAct F16 outcomes are unchanged: the D-F16-0000 storm (1 of 8 completed, 7 confirmed failures; declared not completable), the
CRM outage and the email storm escalated, and none had a duplicate.
D-F16-0003, the deadline crossing, still escalated: it retried on the same key while
the key was protected, then verified until the deadline. Its post-escalation reopens
went back to escalation. Expected terminal escalated_unresolved; no effect, no
duplicate.

**Harness snapshot race (open; your decision).** The runner stops observing after
every operation has been terminal or escalated for `STABLE_SECONDS`. It then takes
one more snapshot (`chaos/runner.py`, after the observation loop), and that snapshot
is what gets scored. In both B3 cases, an operation that had escalated at 9–10 s was in
a post-escalation reopen (VERIFYING) at that moment. It was scored "pending", while
its final view and every earlier B3 run show ESCALATED_UNRESOLVED. SafeAct reopens the
same way, so it can be hit too.

Only escalated operations can flip: SUCCEEDED and FAILED_CONFIRMED never reopen. So the
race changes neither safety counts nor useful completion, only the reported outcome
class. Two possible fixes:

- score the snapshot that satisfied the stability check, or
- count a reopen of an already escalated operation as still escalated for settling.

Either changes pinned harness code and needs a re-pin. I have not changed it.

## Useful completion by family (safety-failure rows in brackets)

| Family | SafeAct | B3 | B2 | B1 | B0 |
|---|---|---|---|---|---|
| F05 | 2/2 [0] (was 2/2) | 2/2 [0] | 0/2 [2] | 0/2 [2] | 0/2 [0] |
| F06 | 5/5 [0] (was 4/4) | 5/5 [0] | 4/5 [1] | 1/5 [4] | N/A |
| F16 | **5/5** [0] (was 4/5) | 5/5 [0] | 5/5 [0] | 5/5 [0] | 0/5 [0] |

## The three email cases

| Case | Fault | SafeAct | B3 | B0 / B1 / B2 |
|---|---|---|---|---|
| D-F05-0005 | worker killed after the provider sent the email | escalated_unresolved, 1 send, 1 effect | escalated_unresolved, 1 send | pending, 1 send each |
| D-F06-0004 | send held 5 s; the 3 s lease expires while it runs | confirmed_success, 1 send, 1 effect | confirmed_success | B0 N/A; B1/B2 confirmed_success, 1 send |
| D-F16-0005 | 429 storm (6 responses for 4 messages) | 4 x escalated_unresolved, 1 send each, 0 effects | same | pending, 1 send each |

No arm resent an email in any of the three cases, and no arm duplicated one. The
baselines leave both kinds of ambiguous email as `pending` instead of escalating them
to a human. Because none of the 429s was followed by another request,
`rate_limited_then_retried` is correctly not observed for the email storm.

## Coverage in these families

No arm lost a critical label it observed in these families before. B1 and B2 gained
`worker_crashed_after_send`, from the email case. SafeAct and B3 observed the same 7
labels as before.

Saved development rows still never carry a qualification id: 493 rows across the
repository's packets, 78 distinct ids, all `D-`.

## Files

- [comparison.json](comparison.json): differences, plus per-family figures before and
  after. Produced by `../dev-rerun-post-pooling-20261024/compare_rerun.py`.
- The run's own outputs: [results.jsonl](results.jsonl), [summary.json](summary.json),
  [coverage.json](coverage.json), [environment.json](environment.json) and
  [report.md](report.md).
- `raw.jsonl` (38 MB) and the log are in
  `~/safeact-evidence/docs/phase-e/dev-rerun-f05-f06-f16-20261024/`; their digests
  are in [outside-repo.json](outside-repo.json).
- [artifact-sha256.json](artifact-sha256.json) seals this packet.
