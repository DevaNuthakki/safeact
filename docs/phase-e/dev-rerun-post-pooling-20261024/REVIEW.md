# Post-pooling development rerun (fb89604)

Your go (2026-10-24): re-run the existing development cases, not the frozen corpus,
on the committed post-pooling system and compare each outcome with the pre-pooling
rows. **No frozen smoke, calibration or full run was started.** No real Stripe or paid
model call was made.

| | |
|---|---|
| System under test | commit `fb89604` (detached worktree, clean tree) |
| Command | `SAFEACT_RUN_QUALIFICATION=1 python -m chaos.qualify --corpus dev --arms safeact,b0,b1,b2,b3 --slots 1` |
| Cases | 75 development cases (seed 7) x 5 arms = 375 rows; 0 harness errors; exit 0; 4,069 s |
| Baseline | the 375 selected pre-pooling rows (`development-selected-results.jsonl`, dev-baseline-adapter-20261023) |
| Machine | MacBookPro15,1, 12 CPUs; 1-min load 2.73 at start, 1.66 at end; AC at start, **battery (72%) at end**; caffeinate not active |

## Result

375 pairs compared on outcome fields: N/A, every oracle safety count, integrity,
harness errors, cleanup, per-operation outcome class, effects, safe completion and
observed interleavings. Timing values are never compared. **6 pairs differ. None
changes a safety count, an effect count, integrity or a harness error, in any arm.**
Each scenario seed is the corpus seed 7, applied per (split, family, index).

| Case (seed 7) | Arm | Field | Pre-pooling | Post-pooling | Same outcome in an earlier pre-pooling run |
|---|---|---|---|---|---|
| D-F01-0001 | B1 | outcome class | pending | confirmed_success (2 effects, 1 duplicate both times) | yes (weaker-marker-corrected) |
| D-F01-0001 | B2 | outcome class | confirmed_success | pending (2 effects, 1 duplicate both times) | no; B1 had pending on this case |
| D-F16-0000 | B3 | interleavings | breaker_open | breaker_open + rate_limited_then_retried | each label seen in an earlier run |
| D-F16-0002 | B1 | ops 0-1: class, effects | pending, 0 effects | confirmed_success, 1 effect | yes (breaker, weaker-first) |
| D-F16-0002 | SafeAct | op 0: class, effects | confirmed_success, 1 effect | escalated_unresolved, 0 effects | **yes (safeact-first)** |
| D-F16-0003 | B3 | interleavings | none | provider_outage_spanned_deadline | yes (b3-audit) |

Every saved pre-pooling run of each pair is in [difference-history.json](difference-history.json).

**SafeAct, D-F16-0002 (5 payments, one 5.99 s provider outage).** Safe, but a usefulness
miss (4/5). Operation 0's second dispatch started at +5.56 s, inside the outage. The
post-outage lookup proved that attempt not applied. The first attempt stayed
`MAY_HAVE_DISPATCHED`, so negative coverage was incomplete (`core/attempts.py`
`assess_negative_coverage`). The reconciler then verified until the investigation
deadline and escalated (`negative_evidence_coverage_incomplete`), with no retry and no
effect. In the selected pre-pooling row, every reclaim landed after the outage. The
reclaim time comes from the backoff jitter, which is derived from md5(operation id,
attempt) (0018), and operation ids are fresh per run. The same escalation occurred in
the pre-pooling `safeact-first` run, so this outcome predates pooling.

**B1/B2, D-F01-0001 (CRM, 1.5 s downstream timeout).** Both blind-retry baselines
duplicate the effect every time. Whether the final local state reads pending or
confirmed depends on whether the retry's response lands inside the toxic window.

Per family, SafeAct and B3 match the pre-pooling rows everywhere except F16, where
SafeAct is 4/5 (was 5/5). Both arms have 0 safety-failure rows. By arm:

| Arm | Useful / achievable | Safety-failure rows | N/A rows | Duplicates |
|---|---:|---:|---:|---:|
| SafeAct | 61/62 | 0 | 0 | 0 |
| B3 | 59/59 | 0 | 3 (F15) | 0 |
| B2 | 42/49 | 7 | 14 | 7 |
| B1 | 34/49 | 16 | 14 | 14 |
| B0 | 7/24 | 12 | 40 | 13 |

Coverage: SafeAct observed 28/28 applicable critical interleavings and B3 25/25. B3
gained `rate_limited_then_retried` over the selected rows. B0, B1 and B2 match their
pre-pooling counts (7/10, 16/18, 16/18).

## Files

[comparison.json](comparison.json) has the differences and the per-family figures,
pre and post. It was produced by [compare_rerun.py](compare_rerun.py), which reports
0 differences when the selected rows are compared with themselves.
The run's own outputs are [results.jsonl](results.jsonl), [summary.json](summary.json),
[coverage.json](coverage.json), [environment.json](environment.json) and
[report.md](report.md).

`raw.jsonl` (66 MB) and the run log are kept outside the repo, in
`~/safeact-evidence/docs/phase-e/dev-rerun-post-pooling-20261024/`. Their digests are in
[outside-repo.json](outside-repo.json). [artifact-sha256.json](artifact-sha256.json)
seals this packet.

The scanner allowlist commit after `fb89604` (`deploy/evaluation/agent_v1/secret_scan.py`)
changes no code that the run executes.
