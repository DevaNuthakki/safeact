# Full qualification run v1 (primary, headline)

The author's go (2026-10-25), after the announcement **FINAL BIG QUALIFICATION RUN**.
This is the single primary invocation (`../executions/primary-full.json`). Its rows are
the headline result. Diagnostics, if any are run later, never replace them.

| | |
|---|---|
| Manifest | `sha256: b779fa720fc3a71fe9c26cd529f9199a9be0aed2779dbaa2c8c7968522df51af` (system under test at `415d410`; this run's tree `f24bc59` adds docs only) |
| Calibration | 4 slots, per [the amendment](../calibration/amendment.json) |
| Command | `SAFEACT_RUN_FROZEN_QUALIFICATION=1 python -m chaos.qualify --manifest chaos/manifest_qual_v1.json --scope full --slots 4 --calibration docs/phase-e/qual-v1/calibration/amendment.json --out docs/phase-e/qual-v1/full` |
| Scope | 1,200 seeded scenarios (seed 510112026) x 5 arms (SafeAct, B0-B3) = **6,000/6,000 rows**; 0 not run; 0 error rows; not stopped for budget or safety; exit 0 |
| Duration | 18,234 s (5.07 h), 2026-10-25 03:54-09:01 UTC |
| Machine | MacBookPro15,1, i7-8850H, 12 CPUs, 32 GB, macOS 15.4.1. AC at start and end; the run's own `caffeinate -i -w <pid>` assertion was observed at the end |
| External cost | 0 real Stripe calls, 0 paid model calls, $0 |

## Headline

Prohibited effects are counted per intended business action. An effect is a
duplicate when one intent has more than one effect, whichever operation produced it.

| Arm | Safety-failure rows | Prohibited duplicates | Orphans | Lost intents (unaccounted) | Changed accepted | False claims | Useful / achievable | N/A rows |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **SafeAct** | **0** | **0** | **0** | **0** | **0** | **0** | **924/937 (98.6%)** | 0 |
| B3 | 0 | 0 | 0 | 0 | 0 | 0 | 841/874 (96.2%) | 63 |
| B2 | 92 | 92 | 0 | 0 | 0 | 12 | 558/691 (80.8%) | 294 |
| B1 | 239 | 213 | 0 | 0 | 0 | 25 | 426/691 (61.6%) | 294 |
| B0 | 250 | 371 | 0 | 0 | 64 | 25 | 149/438 (34.0%) | 556 |

Compensation duplicates: B0 39, B1 39, all other arms 0.

**The stop condition was not met.** SafeAct shows 0 prohibited duplicates, 0 orphan
effects and 0 lost intents across all 1,200 scenarios.

The claim stays within the declared tested contracts: zero observed prohibited duplicate
effects in these scenarios. It is not universal exactly-once.

**Coverage.** SafeAct observed 28 of its 28 applicable critical interleavings and B3
observed 25 of 25. B2 and B1 each observed 17 of 18 (missing:
compensation_unresolved_after_lost_response). B0 observed 7 of 10 (missing:
changed_payload_refused, compensation_unresolved_after_lost_response and
duplicate_submissions_collapsed). SafeAct's protocol boundaries were all exercised:
commit acknowledgements lost at claim, fence and outcome, and a concurrent reclaim
refused. Details are in [coverage.json](coverage.json) and
[boundary-details.json](boundary-details.json).

## SafeAct's 13 missed completions (6 rows): the declared trade-off

| Scenario | Fault | Missed ops | Outcome of missed ops |
|---|---|---:|---|
| Q-F16-0051 | 429 storm (13 responses) | 4 | confirmed_failure, 0 effects |
| Q-F16-0052 | 429 storm (9) | 1 | confirmed_failure, 0 effects |
| Q-F16+F11-0010 | 429 storm (9) + commit ack lost after fence | 2 | confirmed_failure, 0 effects |
| Q-F16+F11-0011 | 429 storm (10) + commit ack lost after fence | 2 | confirmed_failure, 0 effects |
| Q-F16+F11-0018 | 429 storm (11) + commit ack lost after fence | 2 | confirmed_failure, 0 effects |
| Q-F16+F11-0013 | 5.5 s outage + commit ack lost after claim | 2 | escalated_unresolved, 0 effects |

This is the trade-off accepted for D-F16-0002. A fixed attempt budget can be spent
inside a storm or a short outage. SafeAct then ends with proven non-application
(confirmed_failure) or escalates to a human, with no effect. A luckier retry timing
would complete. A candidate improvement for a later version, **not evaluated**: do not
count attempts the transport proves were never sent, or make attempt accounting
breaker-aware.

## Per family: useful / achievable [safety-failure rows]

| Family | Scenarios | SafeAct | B3 | B2 | B1 | B0 |
|---|---:|---|---|---|---|---|
| CONTROL | 60 | 60/60 [0] | 60/60 [0] | 60/60 [0] | 60/60 [0] | 60/60 [0] |
| F01 | 64 | 51/51 [0] | 51/51 [0] | 39/51 [12] | 0/51 [51] | 0/51 [51] |
| F02 | 64 | 64/64 [0] | 64/64 [0] | 64/64 [0] | 64/64 [0] | 64/64 [0] |
| F03 | 64 | 43/43 [0] | 43/43 [0] | 26/43 [17] | 0/43 [43] | 0/43 [43] |
| F04 | 64 | 64/64 [0] | 64/64 [0] | 64/64 [0] | 64/64 [0] | 0/41 [41] |
| F04x8 | 20 | 20/20 [0] | 20/20 [0] | 20/20 [0] | 20/20 [0] | 0/13 [13] |
| F05 | 64 | 44/44 [0] | 44/44 [0] | 24/44 [20] | 9/44 [35] | 0/44 [0] |
| F05+F08 | 20 | 18/18 [0] | 18/18 [0] | 10/18 [8] | 7/18 [11] | N/A |
| F06 | 64 | 64/64 [0] | 64/64 [0] | 45/64 [19] | 15/64 [49] | N/A |
| F06+F07 | 20 | 20/20 [0] | 20/20 [0] | N/A | N/A | N/A |
| F07 | 64 | 64/64 [0] | 64/64 [0] | N/A | N/A | N/A |
| F08 | 64 | 64/64 [0] | 64/64 [0] | 53/64 [4] | 45/64 [12] | N/A |
| F09 | 64 | 0/0 [0] | 0/0 [0] | 0/0 [0] | 0/0 [0] | 0/0 [0] |
| F09+F03 | 20 | 0/0 [0] | 0/0 [0] | 0/0 [0] | 0/0 [0] | 0/0 [0] |
| F10 | 64 | 64/64 [0] | 64/64 [0] | 64/64 [0] | 64/64 [0] | 0/64 [64] |
| F11 | 64 | 52/52 [0] | 52/52 [0] | 47/52 [0] | 47/52 [0] | N/A |
| F12 | 64 | 27/27 [0] | 27/27 [0] | N/A | N/A | N/A |
| F13 | 63 | 25/25 [0] | 25/25 [0] | 25/25 [12] | 12/25 [38] | 12/25 [38] |
| F13+F12 | 20 | 9/9 [0] | 9/9 [0] | N/A | N/A | N/A |
| F14 | 63 | 63/63 [0] | 63/63 [0] | N/A | N/A | N/A |
| F15 | 63 | 63/63 [0] | N/A | N/A | N/A | N/A |
| F16 | 63 | 28/33 [0] | 18/33 [0] | 12/33 [0] | 14/33 [0] | 13/33 [0] |
| F16+F11 | 20 | 17/25 [0] | 7/25 [0] | 5/25 [0] | 5/25 [0] | N/A |

**F09** is declared not completable. The payment key's retention expires before any
lookup answers, so a retry would be unprotected, and the safe result is not to retry. In an N/A cell, the arm lacks the capability the family
tests, for example agent boundaries (F15) or human review (F14). Such rows are counted
separately, never as passes.

By connector, SafeAct's useful completion was:

| Connector | Useful / achievable |
|---|---:|
| CRM | 367/367 |
| Email | 139/139 |
| Payments | 418/431 |

SafeAct had 0 safety-failure rows on every connector.

## Load and the frozen load-suspect rule

- **Load during the run.** 126 of 9,586 sampled 1-minute loads (1.3%) reached or
  exceeded the 12 CPUs. The maximum was 22.74, against 9.84 in the 4-slot calibration.
  The high samples are spread across all arms (SafeAct 24 of them), and no SafeAct row
  failed.
- **The rule.** A row is load-suspect if it has a safety failure or harness error and
  either belongs to a timing-sensitive family (F05, F06, F07, F08, F11 or F16) or has a
  load sample above the core count.
- **Result.** [load-suspect.json](load-suspect.json) flags 169 rows: 158 for a
  timing-sensitive family and 11 for load above the core count. They are B1 107, B2 51
  and B0 11. **No SafeAct or B3 row is flagged**, because neither arm has a failing row.
- **What this means.** Under the frozen rule (`headline_replacement: false`), these
  baseline failures stay in the headline. A diagnostic rerun (1 slot, idle machine)
  could show whether load contributed. It would be reported beside the headline,
  never instead of it. **No diagnostic rerun has been run.** It needs the author's go.

## Unstable at scoring

16 B3 rows had a later snapshot that differed from the scored one. Both states are on
each row, and none was rescored. SafeAct, B0, B1 and B2 had 0 such rows.

## Files

Committed (per the storage decision in `../../evidence-index.md`):

- **Run outputs:** [report.md](report.md), [summary.json](summary.json),
  [coverage.json](coverage.json), [environment.json](environment.json),
  [boundary-details.json](boundary-details.json) and [load-suspect.json](load-suspect.json).
- **Scored rows:** [results.jsonl](results.jsonl) has all 6,000 scored rows (7 MB),
  without raw traces. It is kept so that every summary figure can be recomputed from
  the repository.
- **Failure rows:** [failure-rows.jsonl](failure-rows.jsonl) holds the 700 rows that
  have a safety failure, harness error, not-run, useful-completion miss or unstable
  scoring, each tagged with `selected_for`.
- **Traces:** [traces-failing-unstable.jsonl](traces-failing-unstable.jsonl) holds the
  raw rows of those 700 (transition events, provider timeline, HTTP, broker, ledger,
  admissions, failpoints, load, interleavings).
  - Omitted: the bulky `database`, `execution` and `infrastructure` sample streams, each
    listed in `omitted_fields`.
  - They are in the full raw file.
- **Digests:** [outside-repo.json](outside-repo.json) has digests of `raw.jsonl`
  (1,196,365,366 bytes) and its compressed copy `raw.jsonl.gz` (55,115,769 bytes; it
  decompresses to the same digest), plus the run log.
  - The compressed copy is in `~/safeact-evidence/docs/phase-e/qual-v1/full/`.
  - It is planned as a GitHub Release asset on the public repository; not yet published.
  - `raw.jsonl` also stays in the working tree, gitignored, for any later diagnostics.
- [artifact-sha256.json](artifact-sha256.json) seals this packet.
