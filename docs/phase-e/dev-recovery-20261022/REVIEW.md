# M5.9 development recovery evidence, 2026-10-22

This is a development run of uncommitted M5.9 code based on `3269385`,
not a frozen qualification run. Only the SafeAct arm ran. No Stripe or model
calls were made. Source digests are in [source-sha256.json](source-sha256.json);
artifact digests are in [artifact-sha256.json](artifact-sha256.json).

## Run and independent replay

```sh
SAFEACT_RUN_QUALIFICATION=1 .venv/bin/python -m chaos.qualify \
  --corpus dev --families CONTROL,F07,F09,F16 --slots 1 --out /tmp/recovery-new
```

Sixteen cases finished in 394.7 seconds with exit 0. There were no harness
errors, observed safety failures, projection drift or incomplete quiescence.
The run occurred on a local Mac while regression suites used separate schemas
and reserved Redis databases on the same machine. Environment/load samples
are retained in [environment.json](environment.json) and each raw row.
These timings are not load-test results or slot calibration.

After execution, every result was recalculated from [raw.jsonl](raw.jsonl)
using `Scenario.from_json`, `oracle.evaluate` and `coverage.observed`. JSON
normalization accounted for tuple/list serialization. All 16 recalculated
rows exactly matched [results.jsonl](results.jsonl). Recalculated aggregates,
rendered report and coverage counts also exactly matched
[summary.json](summary.json), [report.md](report.md) and
[coverage.json](coverage.json). All final quiescence flags were complete,
and the existing evaluation secret scanner reported no findings.

## Safety and useful completion

| Family | Cases | Useful / achievable operations | Final provider effects | Outcomes |
|---|---:|---:|---:|---|
| CONTROL | 3 | 3/3 | 3 | 3 confirmed successes |
| F07 | 5 | 5/5 | 5 | 5 confirmed successes |
| F09 | 3 | N/A: 0 achievable | 3 | 3 unresolved escalations; no unsupported replay after key expiry |
| F16 | 5 | 6/14 | 6 | 6 confirmed successes, 7 confirmed failures, 5 unresolved escalations |
| Total | 16 | 14/22 | 17 | Zero observed duplicates, orphans, false claims or lost admitted operations |

The original F16 storm schedule remains unchanged: case `D-F16-0000`
completed 1/8 operations; the other seven exhausted their protected attempt
budgets after rejection. The added payment outage case `D-F16-0003` also
failed useful completion, escalating without an effect. Its completion
denominator remains achievable because a scheduler could conserve attempts
until recovery. Neither result is removed or relabeled as useful completion.

## Directly observed recovery coverage

| Interleaving | Cases credited |
|---|---:|
| Empty HTTP lookup returned before a later provider commit | 4 |
| First provider effect followed an independently observed committed escalation | 2 |
| Actual stored payment key expired while the operation was unresolved | 3 |
| HTTP 429 followed by another dispatch | 1 |
| Continuous provider outage bracketed live/passed samples of one investigation deadline | 3 |

All three original F07 cases received the intended empty HTTP lookup before
the held effect committed. This closes the earlier development-fixture gap:
`q-v2-eventual` payment keys now use the delayed lookup index, while strong
keys retain in-progress visibility and native-key execution protections.
Both payment and CRM post-escalation commit variants were observed.

Coverage uses the observation window. The final ledger additionally includes
effects drained during cleanup; those effects still count for safety but
cannot establish a timed fault interleaving. This selected run does not
exercise the other critical interleavings, which remain explicitly absent
in its coverage table.

## Still pending before M5.9 can close

- Workflow/compensation effect attribution and an executable F13 path.
- Real review-race and agent-boundary scenarios for F14/F15 in this harness;
  their earlier integration tests do not substitute for qualification cases.
- Remaining declared interleavings, including breaker-open reconciliation
  and stale approvals, with direct evidence rather than scheduled intentions.
- Final achievable-completion and scoring review before the corpus freeze.

B0–B3 belong to M5.10. Corpus/manifest freeze and STOP 2 review belong to
M5.11. Full qualification, load testing and packaging have not been completed.
