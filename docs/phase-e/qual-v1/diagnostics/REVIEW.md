# Load-suspect diagnostic rerun (beside the headline, never instead of it)

The author's go (2026-10-25). The frozen load-suspect rule flagged 169 baseline rows
in the primary full run. This rerun repeats their 118 scenarios on all five arms at
1 slot on an idle machine, to show whether load contributed. **The headline stays the
primary run in [../full/](../full/REVIEW.md).** This run has `headline_replacement:
false`, its own invocation claim, and its rows never join or replace the primary rows.

| | |
|---|---|
| Manifest | `sha256: b779fa720fc3a71fe9c26cd529f9199a9be0aed2779dbaa2c8c7968522df51af` (tree `7e1409b`, docs only after `415d410`) |
| Command | `SAFEACT_RUN_FROZEN_QUALIFICATION=1 python -m chaos.qualify --manifest chaos/manifest_qual_v1.json --scope diagnostic --slots 1 --diagnostics-from docs/phase-e/qual-v1/full --out docs/phase-e/qual-v1/diagnostics` |
| Invocation | [../executions/diagnostic-1792919464161532000-13043.json](../executions/diagnostic-1792919464161532000-13043.json) |
| Primary originals | pinned in [environment.json](environment.json): primary raw `5faff691…`, results `46364fe0…`, environment `92e08964…` |
| Scope | 118 scenarios x 5 arms = **590/590 rows**; 0 not run; 0 error rows; exit 0 |
| Duration | 5,945 s (1.65 h), 2026-10-25 09:10-10:50 UTC |
| Load | 1,036 sampled 1-minute loads, maximum **6.65**, none at or above the 12 CPUs (primary run: maximum 22.74) |
| Machine | the same MacBookPro15,1 (12 CPUs), AC at start and end, the run's own `caffeinate` assertion observed at the end |
| External cost | 0 real Stripe calls, 0 paid model calls, $0 |

## Result

**590 of 590 (scenario, arm) pairs match the primary rows on every compared outcome
field**: prohibited duplicates, orphans, unaccounted intents, changed-accepted, false
claims, each operation's outcome class, effect counts, harness errors and N/A status.
Timing is not compared.

- **All 169 flagged baseline failures recur** at 1 slot with the same counts. Load did
  not cause them; they come from the baselines' missing protections.
- **The 421 companion rows** (SafeAct and B3 on every scenario, plus the baseline rows
  that passed) are unchanged. SafeAct and B3 again have 0 safety-failure rows and
  118/118 useful completion each.

| Family | Arm | Primary reason | Flagged rows | Failed again at 1 slot |
|---|---|---|---:|---:|
| F05 | B1 / B2 | timing-sensitive family | 35 / 20 | 35 / 20 |
| F05+F08 | B1 / B2 | timing-sensitive family | 11 / 8 | 11 / 8 |
| F06 | B1 / B2 | timing-sensitive family | 49 / 19 | 49 / 19 |
| F08 | B1 / B2 | timing-sensitive family | 12 / 4 | 12 / 4 |
| F04, F04x8, F10 | B0 | load above core count | 11 | 11 |

The 11 B0 rows were flagged in the primary run only because a load sample there
reached the core count. Here no sample did, so this run's own
[load-suspect.json](load-suspect.json) lists only the 158 timing-sensitive-family
rows, and those 11 failures stand as unflagged failures with an identical outcome.

## What this does and does not change

- **Unchanged:** the headline table, every count and every flag in `../full/`. The
  primary packet is not edited. Its line "No diagnostic rerun has been run" was true
  when it was sealed; this packet is the later diagnostic it refers to.
- **What it adds:** for these 118 scenarios, the baseline safety failures reproduce at
  1 slot with peak load 6.65. That supports reading them as properties of B0-B2, not
  load artifacts. It does not test SafeAct under higher load than the primary run did.

## Files

- **Run outputs:** [report.md](report.md), [summary.json](summary.json),
  [results.jsonl](results.jsonl) (590 scored rows), [coverage.json](coverage.json),
  [environment.json](environment.json), [boundary-details.json](boundary-details.json)
  and [load-suspect.json](load-suspect.json).
- **Comparison:** [comparison.json](comparison.json) lists each pair with its primary and
  diagnostic fields. It is produced by [compare_diagnostic.py](compare_diagnostic.py),
  run from the repository root.
- **Outside the repo:** [outside-repo.json](outside-repo.json) has digests of `raw.jsonl`
  (48 MB, gitignored, kept in the working tree) and its compressed copy `raw.jsonl.gz`
  (3 MB; decompresses to the same digest), plus the run log. Both are in
  `~/safeact-evidence/docs/phase-e/qual-v1/diagnostics/`.
- [artifact-sha256.json](artifact-sha256.json) seals this packet.
