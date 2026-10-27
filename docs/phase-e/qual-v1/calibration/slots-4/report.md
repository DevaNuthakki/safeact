# SafeAct qualification evidence

calibration

Safety failures and useful completion are reported separately. A zero exit status checks
harness validity and SafeAct safety; it does not assert useful completion or full coverage.

Useful completion counts requested operations whose declared acceptable terminal and
independent effect count agree, divided by all operations declared achievable.

A requested workflow is one completion unit; its forward steps and inverse retain separate
identities, evidence and effect counts in the raw results and per-step scores.

Refusals and errored cases stay in that denominator. N/A rows are excluded and counted separately.

The final ledger includes effects drained during cleanup. Coverage and completion use the
observation window; cleanup releases do not establish a scheduled interleaving.

Rows are scored from the snapshot that passed the stability check. A row whose state
changed in a later snapshot (window close or final) keeps both states and is counted as
unstable at scoring.

Declared trade-off: the attempt budget is fixed. A short provider outage can spend every
attempt, so SafeAct escalates with 0 effects where a luckier retry timing completes; retry
timing comes from per-operation jitter, so this varies between runs. A candidate improvement
for a later version (not evaluated): do not count attempts the transport proves were never
sent, or make attempt accounting breaker-aware.

## By arm

| Group | Useful / achievable | Safety-failure rows | Error rows | Unstable at scoring | Effects | Duplicates | Orphans | N/A rows |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| b0 | 11/31 | 19 | 0 | 0 | 75 | 32 | 0 | 38 |
| b1 | 31/47 | 17 | 0 | 0 | 76 | 15 | 0 | 20 |
| b2 | 40/47 | 7 | 0 | 0 | 69 | 7 | 0 | 20 |
| b3 | 58/58 | 0 | 0 | 2 | 75 | 0 | 0 | 4 |
| safeact | 62/62 | 0 | 0 | 0 | 80 | 0 | 0 | 0 |

## By family

| Group | Useful / achievable | Safety-failure rows | Error rows | Unstable at scoring | Effects | Duplicates | Orphans | N/A rows |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| CONTROL | 15/15 | 0 | 0 | 0 | 15 | 0 | 0 | 0 |
| F01 | 10/20 | 10 | 0 | 0 | 30 | 10 | 0 | 0 |
| F02 | 20/20 | 0 | 0 | 0 | 10 | 0 | 0 | 0 |
| F03 | 11/20 | 9 | 0 | 0 | 29 | 9 | 0 | 0 |
| F04 | 16/18 | 2 | 0 | 0 | 23 | 5 | 0 | 2 |
| F04x8 | 8/10 | 2 | 0 | 0 | 24 | 14 | 0 | 0 |
| F05 | 4/10 | 4 | 0 | 0 | 21 | 4 | 0 | 0 |
| F05+F08 | 8/8 | 0 | 0 | 0 | 8 | 0 | 0 | 2 |
| F06 | 12/16 | 4 | 0 | 0 | 20 | 4 | 0 | 4 |
| F06+F07 | 4/4 | 0 | 0 | 0 | 4 | 0 | 0 | 6 |
| F07 | 8/8 | 0 | 0 | 0 | 8 | 0 | 0 | 12 |
| F08 | 15/16 | 1 | 0 | 0 | 17 | 1 | 0 | 4 |
| F09 | 0/0 | 0 | 0 | 0 | 10 | 0 | 0 | 0 |
| F09+F03 | 0/0 | 0 | 0 | 0 | 10 | 0 | 0 | 0 |
| F10 | 16/20 | 4 | 0 | 0 | 24 | 4 | 0 | 0 |
| F11 | 14/16 | 0 | 0 | 0 | 14 | 0 | 0 | 4 |
| F12 | 0/0 | 0 | 0 | 2 | 8 | 0 | 0 | 12 |
| F13 | 8/10 | 7 | 0 | 0 | 54 | 3 | 0 | 0 |
| F13+F12 | 2/2 | 0 | 0 | 0 | 8 | 0 | 0 | 6 |
| F14 | 8/8 | 0 | 0 | 0 | 8 | 0 | 0 | 12 |
| F15 | 4/4 | 0 | 0 | 0 | 4 | 0 | 0 | 16 |
| F16 | 19/20 | 0 | 0 | 0 | 19 | 0 | 0 | 0 |
| F16+F11 | 0/0 | 0 | 0 | 0 | 7 | 0 | 0 | 2 |

## By connector

| Group | Useful / achievable | Safety-failure rows | Error rows | Unstable at scoring | Effects | Duplicates | Orphans | N/A rows |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| crm | 67/88 | 19 | 0 | 2 | 135 | 31 | 0 | 55 |
| email | 20/23 | 1 | 0 | 0 | 22 | 1 | 0 | 2 |
| payments | 115/134 | 23 | 0 | 0 | 218 | 22 | 0 | 25 |

## Outcomes by arm

- b0: {"confirmed_failure": 2, "confirmed_success": 25, "pending": 20, "verified_compensation": 1}
- b1: {"confirmed_failure": 2, "confirmed_success": 45, "pending": 25, "verified_compensation": 1}
- b2: {"confirmed_failure": 2, "confirmed_success": 50, "pending": 20, "verified_compensation": 1}
- b3: {"confirmed_failure": 6, "confirmed_success": 54, "escalated_unresolved": 26, "pending": 1, "verified_compensation": 2}
- safeact: {"confirmed_failure": 2, "confirmed_success": 59, "escalated_unresolved": 29, "pending": 1, "verified_compensation": 2}

Effect attribution by arm:

- b0: 70 forward, 5 compensation, 0 orphan; 3 duplicate compensation effects and 0 invalid compensation effects.
- b1: 71 forward, 5 compensation, 0 orphan; 3 duplicate compensation effects and 0 invalid compensation effects.
- b2: 67 forward, 2 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.
- b3: 71 forward, 4 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.
- safeact: 76 forward, 4 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.

Provider activity and cost basis:

- b0: 114 mock calls (114 dispatch, 0 lookup); external provider cost $0.00.
- b1: 127 mock calls (127 dispatch, 0 lookup); external provider cost $0.00.
- b2: 133 mock calls (133 dispatch, 0 lookup); external provider cost $0.00.
- b3: 478 mock calls (142 dispatch, 336 lookup); external provider cost $0.00.
- safeact: 558 mock calls (148 dispatch, 410 lookup); external provider cost $0.00.

Mock qualification providers have no external charge; provider calls are counted from provider-side observations and cost is recorded as $0.00, not extrapolated to Stripe or other real providers.

Latency distributions (including verification delay) and database-evidence row counts are in summary.json.

Scoring is frozen to the verified input manifest.
