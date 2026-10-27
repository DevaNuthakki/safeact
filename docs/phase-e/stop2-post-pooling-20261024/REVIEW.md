# STOP 2 — PRE-RUN REVIEW (post-pooling system)

This packet replaces the pre-pooling STOP 2 packet
([dev-baseline-adapter-20261023](../dev-baseline-adapter-20261023/REVIEW.md)) as
the review of record. The system under test is now committed and pushed, so
this review is anchored to a commit as well as to file digests.

**Nothing in this packet executed a qualification scenario.** Frozen smoke,
calibration and the full run have not started. No real Stripe or paid model
call was made. Each of them still needs your explicit go, and the final run will
be announced as **FINAL BIG QUALIFICATION RUN** before it starts.

## What you are approving

| | |
|---|---|
| System under test | commit `fb89604` on `feature/m5-sandbox-qualification` (pushed) |
| Candidate | [chaos/manifest_qual_v1.json](../../../chaos/manifest_qual_v1.json), `sha256: ef427f4cb9bb7d384c657c51a383eebbfe96bcb1cae884b02346e5eb653585c2`, anchored in `chaos/qualification_pin.py` |
| Status in the manifest | `awaiting_STOP_2`; production execution false; 0 Stripe and 0 paid-model calls; external and model cost budget $0 |
| Corpus | 1,200 scenarios (60 controls, 1,020 single-family F01–F16, 120 held-out combinations), seed 510112026, 80-case smoke sample |
| Arms | SafeAct, B0, B1, B2, B3 (all five run every scenario; N/A declared in advance) |
| Budget | 6,000 primary scenario/arm rows, one primary invocation, 24 h wall time, at most 7 slots |
| Calibration | after approval: smoke at 1, 2, 4, 6, 7 slots; choose the highest with every sampled 1-minute load strictly below the CPU count; outcomes unused |

Details: [inputs.json](inputs.json).

## What changed since the pre-pooling review

The corpus digest is identical, and so are the scenario, contract, combination,
coverage, applicability, timing, scoring, oracle, workflow-oracle and generator
digests. Only source changed: 16 of 231 pinned files.

1. **Connection pooling** (your instruction, 2026-10-24). Bounded per-role,
   per-process pools in the API, worker, relay, reconciler and advancer. No SQL,
   transaction boundary, authorization or claim/fence/outcome ordering changed.
   Evidence: [pooling packet](../dev-pooling-integration-20261024/REVIEW.md).
2. **Admission conflict classification** (`storage/submission.py`, your review
   follow-up). The internal admission insert stays untargeted, which the 0022
   race needs. A replay must now match every identity field of the visible
   request-key row. Any other conflict is re-raised by PostgreSQL itself in an
   always-rolled-back probe and refused with SQLSTATE 23505 and the constraint
   name. It never returns success without a row. New tests cover the request
   key, provider key and primary key conflicts and the vanished-conflict path.
3. **Formatting only**: `connectors/base.py` (`completed_lookup`) and its call site
   in `storage/experimental_recovery_worker.py`. A trailing blank line was also
   removed from two `chaos/` files.

Files changed since the post-pooling amendment: `chaos/baselines/arm.py`,
`chaos/recovery_faults.py`, `connectors/base.py`,
`storage/experimental_recovery_worker.py`, `storage/submission.py`. The re-pin used
`chaos.freeze_inputs --replace-unreviewed`. That is allowed only while no
execution ledger exists, and none exists.

## Verification on the committed system

All checks below ran on commit `fb89604` with a clean working tree
([regression-checks.json](regression-checks.json); logs and their digests are kept
outside the repo, see [evidence index](../evidence-index.md)):

| Check | Result |
|---|---:|
| Phase A | 908 passed |
| PostgreSQL + broker + baseline adapters (incl. isolated Redis), no skips | 715 passed (1,267 s) |
| Isolated security (PostgreSQL 14.23) | 21 passed |
| API end to end | 9 passed |
| Scripted agent harness | 2 passed |
| Console unit / browser end to end | 37 / 14 passed |
| Typecheck, lint, build, OpenAPI and types drift, `git diff --check` | clean |
| Phase A on each of the 7 commits' own tree | all passed |
| Frozen candidate verification (`frozen.verify` in Phase A) | passed |
| Saved development rows, read-only replay with the committed oracle/scoring | 375 rows; all applicable SafeAct/B3 labels observed |
| Saved-data counterexamples | 25 of 25 mutations rejected |
| Historical packet seals (repo + `~/safeact-evidence/`) | 583 of 583 artifacts verified |
| Secret scan (repo docs, evidence logs) | clean |

The comparison adapters, their applicability and the development results by arm
are unchanged from the pre-pooling review, which describes them in full.

## What this evidence does not establish

- **No scenario has run on the post-pooling system.** The 375 development rows
  were executed before pooling. Replaying them checks the saved bytes against the
  current oracle and scoring code. It does not re-execute SafeAct. Post-pooling
  execution evidence so far is the full regression suite plus the pooling
  packet's local latency check (400 mock operations, zero errors or duplicate
  effects).
- The hosted qualification workflow (`qualification.yml`) has been parsed but
  never dispatched.
- Retained usefulness failures stay as results: the F16 storm (1/8 useful
  completion) and the payment deadline-outage case.
- Timing-sensitive families (F05, F06, F07, F08, F11, F16) will run on a laptop.
  The load-suspect rule is frozen in the manifest, and reruns never replace
  originals.

## Decisions for you

1. **Approve this candidate for STOP 2**, or ask for changes.
2. **Post-pooling development check before smoke (my recommendation).** Re-run
   the existing development cases, not the frozen corpus, on the committed system
   and compare with the pre-pooling rows. This is the cheapest way to see that
   pooling changed no development outcome before any frozen result exists. Under
   your 2026-10-24 instruction it needs your go, like pre-run.
3. **Calibration and full run**: separately authorized, as planned. The full run
   needs AC power, active caffeinate and the calibrated machine. Workers run on
   macOS with fork; a worker crash is checked for fork safety before it is
   attributed to SafeAct.

## Reproduce

```sh
git checkout fb89604
.venv/bin/python -m unittest tests.phase_a.test_qualification_freeze
# replay / counterexamples need the raw files: copy the packet and
# ~/safeact-evidence/docs/phase-e/dev-baseline-adapter-20261023 into one directory, then
PYTHONPATH=. .venv/bin/python <copy>/replay.py <copy>
PYTHONPATH=. .venv/bin/python <copy>/counterexamples.py <copy>
```

[artifact-sha256.json](artifact-sha256.json) seals this packet.

## Addendum (2026-10-24, after your first review)

The sections above are unchanged and describe the candidate as first submitted.

1. **Candidate re-pinned for the scanner allowlist** (commit `65ec02a`). You asked for
   an exact allowlist instead of editing the sealed `REVIEW-M58.md`. It changes one
   pinned source file, `deploy/evaluation/agent_v1/secret_scan.py`, which frozen
   execution uses. Every other input digest is identical. The manifest is now
   `sha256: de864fbd2a572a6ad15859713a41dbc494e6a05d36f6f25790f8393cf9f9f350`
   (was `ef427f4c…`). No execution ledger exists, so `--replace-unreviewed` was allowed.
2. **Post-pooling development rerun** (decision 2, approved): 375 rows on `fb89604`,
   0 harness errors. 6 of 375 pairs differ from the pre-pooling rows. None changes a
   safety count, effect count, integrity or harness error. SafeAct D-F16-0002 is now
   4/5 useful (a safe escalation that also occurred in a pre-pooling run). Details:
   [dev-rerun-post-pooling-20261024](../dev-rerun-post-pooling-20261024/REVIEW.md).

## Addendum 2 (2026-10-24, after your STOP 2 review)

You approved B3 in principle and four changes, which are now made. The candidate is
re-frozen as manifest `sha256: f2959c17be00043fa19cfea29c63138a3e56510549411e977caf8a1470200010`
(commit `a485860`). No execution ledger exists, so `--replace-unreviewed` was allowed.

1. **D-F16-0002: the native-key window was open, so SafeAct had skipped a protected
   retry** (`30d7bd8`). `q-v2` keeps the 24 h retention and only the margin is scaled
   (2 s). SafeAct escalated at +13.7 s with 1 of 3 attempts left.
   - Proof that the latest attempt was not applied, combined with an earlier unresolved
     attempt, gave incomplete coverage. That path never reached the same-key check,
     which a plain NOT_FOUND gets. Incomplete coverage now falls back to that check
     before the deadline.
   - The SQL retry basis still re-checks the key window and the attempt budget at
     VERIFYING -> RETRY_ELIGIBLE and at the fence (SAT12/SAT13). `core/recovery.py` is
     the only source change; no migration.
2. **Email cells in F05, F06 and F16** (`5a23e89`): 12 / 15 / 12 cells in fixed index
   slots, with the 1,200 total kept.
   - The other 1,161 qualification scenarios and all 75 reviewed development cases
     are byte-identical (digests pinned in `tests/phase_a/test_email_cells.py`).
   - New per-connector counts: F05 payments 24, crm 28, email 12; F06 payments 30,
     crm 19, email 15; F16 payments 23, crm 28, email 12. Overall: payments 539,
     crm 459, email 202.
   - Email F05 and F16 are declared not completable: nothing proves a sent message,
     so it is never resent. Email F06 is declared completable: the held send answers
     inside its 6 s deadline.
   - Smoke: one email cell per family; still 80 cases.
   - Corpus digest `sha256: 596d7a9f624586176ba1aee1c7e0f7faa0c21810bfdbc30cdc41b19144917422`
     (was 15868d4d…). Disjointness from development (ids, seeds, content, resource
     ids) is re-proved in a test; 493 saved development rows carry only `D-` ids.
3. **Oracle: duplicates are already counted per intended business action**
   (`a414398`, tests only, no re-score). Ledger effects are attributed to the scenario's
   target and summed over every operation admitted for it. In the saved development
   rows, B0 split 5 intents into several operations; those carry its 8 counted
   duplicates.
4. **Run environment** (`a485860`). Every frozen run checks two things before it
   creates output or claims its invocation:
   - it refuses battery or unknown power;
   - it starts `caffeinate -i -w <own pid>` and requires that process's pmset
     assertion.
   `power_source`, `caffeinate` and `caffeinate_end` are recorded in environment.json.
   On Linux/CI, caffeinate is recorded as not applicable.

**Rerun of the affected development families** on `a485860`
([dev-rerun-f05-f06-f16-20261024](../dev-rerun-f05-f06-f16-20261024/REVIEW.md)): 85
rows, 0 harness errors, AC power and caffeinate throughout.
- SafeAct D-F16-0002 is now 5/5 with one effect per operation.
- No arm resent an email or duplicated one.
- Open for your decision: a harness snapshot race scored two escalated B3 operations
  as `pending` (their final state is escalated). It cannot change safety counts or
  useful completion.

**Disclosure: per-operation jitter makes reruns differ.**
- Retry backoff jitter is derived from md5(operation id, attempt) (0018). Operation
  ids are fresh UUIDs on every run, so each rerun of a seeded scenario retries at
  different moments.
- With faults scheduled at fixed offsets (outages, 429 windows, toxics), that can
  change which recovery path a run takes. Examples: whether a reclaim lands inside an
  outage (D-F16-0002), or whether a blind retry's response lands inside a toxic
  window (B1/B2 D-F01-0001).
- The seed fixes the scenario, not the interleaving. Each row therefore records its
  actual interleaving: the transition log, provider and HTTP traces, and the observed
  critical labels. Coverage and scoring read those, never the seed.
- Development comparisons between runs report such differences per pair. The frozen
  run executes each scenario/arm once, and its originals are the headline; reruns
  never replace them.

Calibration has not started and waits for your go.

## Addendum 3 (2026-10-24, snapshot race)

You approved fixing the snapshot race before calibration. The candidate is re-frozen as
manifest `sha256: 463a5cc633134764224c55c52bcdda9596be2cd6c026b39dbf4ba7f9beeef585`
(commit `c55dbae`). No execution ledger exists, so `--replace-unreviewed` was allowed.

- **The preferred rule was measured and not taken.** Waiting out the post-escalation
  lookup horizon adds 22% observation time on the 304 executed rows of the earlier
  runs. For 14 of 39 rows with escalations, it would also need longer than the frozen
  `settle_seconds`.
- **Rule now in force, the same for every arm.** Score the snapshot that passed the
  stability check, or the window-close snapshot if none did (`scored_from`). If a
  window-close or final snapshot differs from the scored state, record both states on
  the row (`unstable_at_scoring`). Count such rows per arm as "unstable at scoring" in
  the summary (scoring version 3) and the report. They are never dropped or rescored.
- **Full development rerun** on `c55dbae`
  ([dev-rerun-snapshot-20261024](../dev-rerun-snapshot-20261024/REVIEW.md)): 390 rows, 0
  harness errors, AC power and caffeinate throughout. 5 of 390 pairs differ, none in a
  safety count:
  - both B3 race cases are now escalated;
  - the B0 and B2 D-F01-0001 class flips are toxic-window timing with unchanged effects;
  - one row is unstable at scoring (B3 D-F05-0002, recorded);
  - **SafeAct D-F16-0002 op 0 escalated again, with 0 effects.** The native-key retries
    fired, but all three attempts landed inside the 5.99 s outage, which spent the
    attempt budget.

**Calibration was not started.** The D-F16-0002 difference is safe and follows the
declared budget, but it reopens the case you reviewed, and the execution ledger that
calibration creates forbids any later re-pin. Your decision:

- (a) accept it as a retained usefulness result under the jitter disclosure
  (recommended), or
- (b) change the budget policy for provider-outage errors before the freeze.

## Addendum 4 (2026-10-24, your decision on D-F16-0002)

Decision (a): D-F16-0002 is accepted as a retained usefulness result under the jitter
disclosure. The attempt-budget policy is unchanged.

- The generated report (`chaos/report.py`, every run) and the README now state the
  trade-off plainly: a fixed attempt budget can be spent inside a short provider outage,
  so SafeAct escalates with 0 effects where a luckier retry timing completes.
- Both list one candidate improvement for a later version, marked **not evaluated**: do
  not count attempts the transport proves were never sent, or make attempt accounting
  breaker-aware.
- `chaos/report.py` is pinned source, so the candidate is re-frozen once more, before any
  execution ledger exists. The new manifest is
  `sha256: b779fa720fc3a71fe9c26cd529f9199a9be0aed2779dbaa2c8c7968522df51af`. The change
  is report prose only: no scoring, runner, oracle or corpus change, and no rerun needed.
- Calibration runs against this manifest.
