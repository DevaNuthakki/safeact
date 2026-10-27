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

## By arm

| Group | Useful / achievable | Safety-failure rows | Error rows | Effects | Duplicates | Orphans | N/A rows |
|---|---:|---:|---:|---:|---:|---:|---:|
| b0 | 7/24 | 12 | 0 | 47 | 13 | 0 | 40 |
| b1 | 34/49 | 16 | 0 | 78 | 14 | 0 | 14 |
| b2 | 42/49 | 7 | 0 | 68 | 7 | 0 | 14 |
| b3 | 59/59 | 0 | 0 | 70 | 0 | 0 | 3 |
| safeact | 61/62 | 0 | 0 | 73 | 0 | 0 | 0 |

## By family

| Group | Useful / achievable | Safety-failure rows | Error rows | Effects | Duplicates | Orphans | N/A rows |
|---|---:|---:|---:|---:|---:|---:|---:|
| CONTROL | 15/15 | 0 | 0 | 15 | 0 | 0 | 0 |
| F01 | 5/10 | 5 | 0 | 20 | 5 | 0 | 0 |
| F02 | 15/15 | 0 | 0 | 5 | 0 | 0 | 0 |
| F03 | 5/10 | 5 | 0 | 20 | 5 | 0 | 0 |
| F04 | 12/14 | 2 | 0 | 19 | 5 | 0 | 1 |
| F05 | 4/10 | 4 | 0 | 20 | 4 | 0 | 0 |
| F06 | 11/16 | 5 | 0 | 21 | 5 | 0 | 4 |
| F07 | 10/10 | 0 | 0 | 10 | 0 | 0 | 15 |
| F08 | 54/60 | 4 | 0 | 62 | 4 | 0 | 15 |
| F09 | 0/0 | 0 | 0 | 15 | 0 | 0 | 0 |
| F10 | 12/15 | 3 | 0 | 18 | 3 | 0 | 0 |
| F11 | 20/20 | 0 | 0 | 20 | 0 | 0 | 6 |
| F12 | 4/4 | 0 | 0 | 6 | 0 | 0 | 9 |
| F13 | 8/10 | 7 | 0 | 54 | 3 | 0 | 0 |
| F14 | 6/6 | 0 | 0 | 6 | 0 | 0 | 9 |
| F15 | 3/3 | 0 | 0 | 3 | 0 | 0 | 12 |
| F16 | 19/25 | 0 | 0 | 22 | 0 | 0 | 0 |

## By connector

| Group | Useful / achievable | Safety-failure rows | Error rows | Effects | Duplicates | Orphans | N/A rows |
|---|---:|---:|---:|---:|---:|---:|---:|
| crm | 54/72 | 16 | 0 | 93 | 18 | 0 | 36 |
| email | 39/43 | 2 | 0 | 54 | 3 | 0 | 8 |
| payments | 110/128 | 17 | 0 | 189 | 13 | 0 | 27 |

## Outcomes by arm

- b0: {"confirmed_failure": 2, "confirmed_success": 17, "pending": 28, "verified_compensation": 1}
- b1: {"confirmed_failure": 2, "confirmed_success": 49, "pending": 22, "verified_compensation": 1}
- b2: {"confirmed_failure": 2, "confirmed_success": 48, "pending": 23, "verified_compensation": 1}
- b3: {"confirmed_failure": 11, "confirmed_success": 56, "escalated_unresolved": 16, "pending": 1, "verified_compensation": 1}
- safeact: {"confirmed_failure": 9, "confirmed_success": 59, "escalated_unresolved": 18, "pending": 1, "verified_compensation": 1}

Effect attribution by arm:

- b0: 42 forward, 5 compensation, 0 orphan; 3 duplicate compensation effects and 0 invalid compensation effects.
- b1: 73 forward, 5 compensation, 0 orphan; 3 duplicate compensation effects and 0 invalid compensation effects.
- b2: 66 forward, 2 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.
- b3: 68 forward, 2 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.
- safeact: 71 forward, 2 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.

Provider activity and cost basis:

- b0: 107 mock calls (107 dispatch, 0 lookup); external provider cost $0.00.
- b1: 120 mock calls (120 dispatch, 0 lookup); external provider cost $0.00.
- b2: 124 mock calls (124 dispatch, 0 lookup); external provider cost $0.00.
- b3: 283 mock calls (125 dispatch, 158 lookup); external provider cost $0.00.
- safeact: 421 mock calls (138 dispatch, 283 lookup); external provider cost $0.00.

Mock qualification providers have no external charge; provider calls are counted from provider-side observations and cost is recorded as $0.00, not extrapolated to Stripe or other real providers.

Latency distributions (including verification delay) and database-evidence row counts are in summary.json.

Scoring is not frozen. Pending: STOP 2 review and post-review slot calibration.
