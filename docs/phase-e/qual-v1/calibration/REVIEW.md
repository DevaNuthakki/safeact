# Slot calibration amendment (manifest b779fa72)

You authorized this on 2026-10-24, after accepting D-F16-0002 as a retained result.
The frozen 80-case smoke sample (x 5 arms = 400 rows) ran once at each frozen slot
candidate, one candidate after another, on the same machine. **The full qualification
run has not started.** No real Stripe or paid model call was made (each
environment.json records 0 of both).

| | |
|---|---|
| Manifest | `sha256: b779fa720fc3a71fe9c26cd529f9199a9be0aed2779dbaa2c8c7968522df51af` (commit `415d410`) |
| Command | `SAFEACT_RUN_FROZEN_QUALIFICATION=1 python -m chaos.qualify --manifest chaos/manifest_qual_v1.json --scope calibration --slots N --out docs/phase-e/qual-v1/calibration/slots-N` |
| Machine | MacBookPro15,1, Intel i7-8850H, 12 CPUs, 32 GB, macOS 15.4.1 |
| Power | AC at the start and end of every candidate. Each run started its own `caffeinate -i -w <pid>`, and its pmset assertion was still observed at the end |
| Amendment | [amendment.json](amendment.json), written and verified by `python -m chaos.calibrate` |

## Rule and result

The frozen rule: select the highest candidate whose every sampled 1-minute load
(start and end of every applicable row) is strictly below the CPU count, 12. Outcomes
are not used.

| Slots | Wall time | Max sampled load | Eligible | SafeAct safety-failure rows | SafeAct useful | Error rows (all arms) | Unstable at scoring |
|---:|---:|---:|---|---:|---:|---:|---|
| 1 | 4,396 s | 7.99 | yes | 0 | 62/62 | 0 | 0 |
| 2 | 2,243 s | 7.08 | yes | 0 | 62/62 | 0 | 0 |
| **4** | **1,233 s** | **9.84** | **yes (selected)** | 0 | 62/62 | 0 | B3 2 |
| 6 | 971 s | 26.4 | no | 0 | 62/62 | 0 | B3 1 |
| 7 | 925 s | 202.29 | no | 0 | 62/62 | 0 | B3 7 |

**Selected: 4 slots.** At 6 and 7 slots the machine was overloaded, and the 7-slot
peak of 202 is a real sample, not a typo. In the B3 column, the unstable-at-scoring
count grows with load. Those rows are recorded, not rescored, as the snapshot rule
requires.

These candidate outcomes are calibration evidence only. They are not qualification
results and never replace or join the full run.

## Full-run estimate at 4 slots

About **5 hours** of wall time for the 6,000 rows (1,200 scenarios x 5 arms). Two
methods agree:

- **Naive:** 1,233 s per 400 rows, scaled up, gives 5.1 h.
- **Family-weighted:** the 1-slot per-row times, weighted by each family's full-corpus
  count and divided by the measured 4-slot speedup (3.57x), give 5.0 h.

The frozen wall budget is 24 h. Jitter and load make the real duration vary, so plan
for 5–6 h.

## Files

- Per candidate (`slots-N/`): environment, results, summary, coverage, report,
  load-suspect and boundary-details files.
- [../executions/](../executions/): the five calibration invocation claims. The primary
  full-run claim does not exist yet.
- **Raw output stays in the working tree.** Each `slots-N/raw.jsonl` (62–71 MB) is
  gitignored. It stays in place because the full run re-hashes it against the
  amendment before it starts.
- **Copies outside the repo:** the raw files, the per-run logs and the sequence log are
  in `~/safeact-evidence/docs/phase-e/qual-v1/calibration/`. Their digests are in
  [outside-repo.json](outside-repo.json), and each raw digest equals the one pinned in
  the amendment.
