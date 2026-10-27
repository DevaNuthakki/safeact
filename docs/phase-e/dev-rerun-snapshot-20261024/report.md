# SafeAct development evidence

development only; qualification incomplete

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

## By arm

| Group | Useful / achievable | Safety-failure rows | Error rows | Unstable at scoring | Effects | Duplicates | Orphans | N/A rows |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| b0 | 7/24 | 12 | 0 | 0 | 48 | 13 | 0 | 41 |
| b1 | 35/50 | 16 | 0 | 0 | 80 | 14 | 0 | 14 |
| b2 | 43/50 | 7 | 0 | 0 | 70 | 7 | 0 | 14 |
| b3 | 60/60 | 0 | 0 | 1 | 72 | 0 | 0 | 3 |
| safeact | 62/63 | 0 | 0 | 0 | 75 | 0 | 0 | 0 |

## By family

| Group | Useful / achievable | Safety-failure rows | Error rows | Unstable at scoring | Effects | Duplicates | Orphans | N/A rows |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| CONTROL | 15/15 | 0 | 0 | 0 | 15 | 0 | 0 | 0 |
| F01 | 5/10 | 5 | 0 | 0 | 20 | 5 | 0 | 0 |
| F02 | 15/15 | 0 | 0 | 0 | 5 | 0 | 0 | 0 |
| F03 | 5/10 | 5 | 0 | 0 | 20 | 5 | 0 | 0 |
| F04 | 12/14 | 2 | 0 | 0 | 19 | 5 | 0 | 1 |
| F05 | 4/10 | 4 | 0 | 1 | 25 | 4 | 0 | 0 |
| F06 | 15/20 | 5 | 0 | 0 | 25 | 5 | 0 | 5 |
| F07 | 10/10 | 0 | 0 | 0 | 10 | 0 | 0 | 15 |
| F08 | 54/60 | 4 | 0 | 0 | 62 | 4 | 0 | 15 |
| F09 | 0/0 | 0 | 0 | 0 | 15 | 0 | 0 | 0 |
| F10 | 12/15 | 3 | 0 | 0 | 18 | 3 | 0 | 0 |
| F11 | 20/20 | 0 | 0 | 0 | 20 | 0 | 0 | 6 |
| F12 | 4/4 | 0 | 0 | 0 | 6 | 0 | 0 | 9 |
| F13 | 8/10 | 7 | 0 | 0 | 54 | 3 | 0 | 0 |
| F14 | 6/6 | 0 | 0 | 0 | 6 | 0 | 0 | 9 |
| F15 | 3/3 | 0 | 0 | 0 | 3 | 0 | 0 | 12 |
| F16 | 19/25 | 0 | 0 | 0 | 22 | 0 | 0 | 0 |

## By connector

| Group | Useful / achievable | Safety-failure rows | Error rows | Unstable at scoring | Effects | Duplicates | Orphans | N/A rows |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| crm | 54/72 | 16 | 0 | 1 | 93 | 18 | 0 | 36 |
| email | 43/47 | 2 | 0 | 0 | 63 | 3 | 0 | 9 |
| payments | 110/128 | 17 | 0 | 0 | 189 | 13 | 0 | 27 |

## Outcomes by arm

- b0: {"confirmed_failure": 2, "confirmed_success": 16, "pending": 34, "verified_compensation": 1}
- b1: {"confirmed_failure": 2, "confirmed_success": 50, "pending": 27, "verified_compensation": 1}
- b2: {"confirmed_failure": 2, "confirmed_success": 50, "pending": 27, "verified_compensation": 1}
- b3: {"confirmed_failure": 11, "confirmed_success": 57, "escalated_unresolved": 21, "pending": 1, "verified_compensation": 1}
- safeact: {"confirmed_failure": 9, "confirmed_success": 60, "escalated_unresolved": 23, "pending": 1, "verified_compensation": 1}

Effect attribution by arm:

- b0: 43 forward, 5 compensation, 0 orphan; 3 duplicate compensation effects and 0 invalid compensation effects.
- b1: 75 forward, 5 compensation, 0 orphan; 3 duplicate compensation effects and 0 invalid compensation effects.
- b2: 68 forward, 2 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.
- b3: 70 forward, 2 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.
- safeact: 73 forward, 2 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.

Provider activity and cost basis:

- b0: 112 mock calls (112 dispatch, 0 lookup); external provider cost $0.00.
- b1: 126 mock calls (126 dispatch, 0 lookup); external provider cost $0.00.
- b2: 130 mock calls (130 dispatch, 0 lookup); external provider cost $0.00.
- b3: 293 mock calls (131 dispatch, 162 lookup); external provider cost $0.00.
- safeact: 428 mock calls (144 dispatch, 284 lookup); external provider cost $0.00.

Mock qualification providers have no external charge; provider calls are counted from provider-side observations and cost is recorded as $0.00, not extrapolated to Stripe or other real providers.

Latency distributions (including verification delay) and database-evidence row counts are in summary.json.

Scoring is not frozen. Pending: STOP 2 review and post-review slot calibration.
