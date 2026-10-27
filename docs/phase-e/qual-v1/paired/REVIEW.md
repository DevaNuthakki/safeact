# Paired comparison: SafeAct vs B3 (post-hoc, beside the headline)

**This is a post-hoc analysis of the frozen primary run v1.** It re-scores the committed
rows in [../full/results.jsonl](../full/results.jsonl); nothing was re-executed, and no
new run was made. It does not replace the headline in [../full/REVIEW.md](../full/REVIEW.md):
SafeAct 924/937, B3 841/874.

**Why it exists.** The two headline figures have different denominators. B3 has no
guarded agent surface, so its 63 F15 rows are N/A, while SafeAct runs them. This
analysis compares the two arms on the same scenarios, with the same denominator.

| | |
|---|---|
| Written | 2026-10-26, before publishing the repository |
| Input | `full/results.jsonl` (6,000 scored rows; SafeAct and B3 rows only are read) |
| Rule | the frozen scoring code, `chaos.scoring.aggregate` and `chaos.oracle.safety_failures`, applied unchanged to the paired subset |
| Script | [compare_paired.py](compare_paired.py) writes [paired.json](paired.json); `--check` recomputes and compares |
| Test | `tests/phase_a/test_paired_analysis.py` recomputes `paired.json` from the committed rows |

## Which scenarios are paired

- **Paired scenarios:** applicable to both arms. That is 1,137 scenarios with 1,418 requested operations.
- **Achievable:** achievability is declared per scenario, not per arm. On the paired set,
  826 scenarios (874 operations) are achievable, and that is the shared denominator.
- **F09 and F09+F03** (84 paired scenarios) are achievable for no arm, because the native key's
  retention has passed. They count toward safety but not toward usefulness.
- **One-arm coverage:** F15 (63 scenarios, 63 operations), which only SafeAct runs. It is reported
  separately below and is not part of the paired figures.

## Result

| Arm | Safety-failure rows | Prohibited duplicates | Orphans | Lost intents | Changed accepted | False claims | Error rows | Useful / achievable |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| SafeAct | 0 | 0 | 0 | 0 | 0 | 0 | 0 | **861/874 (98.5%)** |
| B3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | **841/874 (96.2%)** |

Compensation duplicates and invalid compensation effects are 0 for both arms.

- **Safety: equal.** Both arms have zero in every safety category on the 1,137 paired
  scenarios.
- **Usefulness:** SafeAct completed 20 more operations out of 874.
  - **Every family except F16 and F16+F11 is equal and complete for both arms.**
  - The whole gap is in F16 (429 storms and short provider outages), alone or combined
    with lost commit acknowledgements (F16+F11).
  - Both arms stopped safely there. B3 ended more operations as confirmed failures: provably not
    applied, nothing charged.
  - In one outage scenario B3 completed one more than SafeAct.

| Family | Paired scenarios | Achievable ops | SafeAct | B3 |
|---|---:|---:|---:|---:|
| F16 | 63 | 33 | 28 | 18 |
| F16+F11 | 20 | 25 | 17 | 7 |
| all other families | 1,054 | 816 | 816 | 816 |

Scenarios where either arm missed an achievable completion:

| Scenario | Achievable ops | SafeAct | B3 |
|---|---:|---:|---:|
| Q-F16-0031 | 6 | 6 | 4 |
| Q-F16-0032 | 8 | 8 | 6 |
| Q-F16-0051 | 8 | 4 | 1 |
| Q-F16-0052 | 7 | 6 | 3 |
| Q-F16+F11-0010 | 5 | 3 | 1 |
| Q-F16+F11-0011 | 6 | 4 | 0 |
| Q-F16+F11-0013 | 3 | 1 | 2 |
| Q-F16+F11-0018 | 7 | 5 | 0 |

In these 8 scenarios the outcomes were:
- **SafeAct:** 37 confirmed successes, 11 confirmed failures, 2 escalated unresolved.
- **B3:** 17 confirmed successes, 33 confirmed failures.
- Neither arm had a single effect counted against it.

## Coverage only one arm has

| Family | Arm that runs it | Why the other is N/A | Scenarios | Safety-failure rows | Useful / achievable |
|---|---|---|---:|---:|---:|
| F15 (agent boundary) | SafeAct | B3: "No guarded agent credential surface" | 63 | 0 | 63/63 |

The headline's 924/937 for SafeAct is exactly the paired 861/874 plus these 63/63.

## Notes

- **Unstable rows.** 16 B3 rows were marked `unstable_at_scoring`. Under the frozen
  snapshot rule they were scored from the stable snapshot, and later differences were
  recorded. None of them is among the 8 scenarios above.
- **What this changes.** Nothing in the headline. It narrows the honest wording to
  "equal on safety; SafeAct completed more under F16 limits; SafeAct also covers F15".
