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
| b0 | 0/7 | 0 | 0 | 5 | 0 | 0 | 5 |
| b1 | 6/12 | 6 | 0 | 22 | 6 | 0 | 0 |
| b2 | 9/12 | 3 | 0 | 19 | 3 | 0 | 0 |
| b3 | 12/12 | 0 | 0 | 13 | 0 | 0 | 0 |
| safeact | 12/12 | 0 | 0 | 14 | 0 | 0 | 0 |

## By family

| Group | Useful / achievable | Safety-failure rows | Error rows | Effects | Duplicates | Orphans | N/A rows |
|---|---:|---:|---:|---:|---:|---:|---:|
| F05 | 4/10 | 4 | 0 | 25 | 4 | 0 | 0 |
| F06 | 15/20 | 5 | 0 | 25 | 5 | 0 | 5 |
| F16 | 20/25 | 0 | 0 | 23 | 0 | 0 | 0 |

## By connector

| Group | Useful / achievable | Safety-failure rows | Error rows | Effects | Duplicates | Orphans | N/A rows |
|---|---:|---:|---:|---:|---:|---:|---:|
| crm | 6/14 | 6 | 0 | 26 | 6 | 0 | 1 |
| email | 4/4 | 0 | 0 | 9 | 0 | 0 | 1 |
| payments | 29/37 | 3 | 0 | 38 | 3 | 0 | 3 |

## Outcomes by arm

- b0: {"confirmed_success": 2, "pending": 26}
- b1: {"confirmed_success": 15, "pending": 18}
- b2: {"confirmed_success": 15, "pending": 18}
- b3: {"confirmed_failure": 9, "confirmed_success": 12, "escalated_unresolved": 10, "pending": 2}
- safeact: {"confirmed_failure": 7, "confirmed_success": 13, "escalated_unresolved": 13}

Effect attribution by arm:

- b0: 5 forward, 0 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.
- b1: 22 forward, 0 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.
- b2: 19 forward, 0 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.
- b3: 13 forward, 0 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.
- safeact: 14 forward, 0 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.

Provider activity and cost basis:

- b0: 56 mock calls (56 dispatch, 0 lookup); external provider cost $0.00.
- b1: 57 mock calls (57 dispatch, 0 lookup); external provider cost $0.00.
- b2: 58 mock calls (58 dispatch, 0 lookup); external provider cost $0.00.
- b3: 112 mock calls (49 dispatch, 63 lookup); external provider cost $0.00.
- safeact: 172 mock calls (59 dispatch, 113 lookup); external provider cost $0.00.

Mock qualification providers have no external charge; provider calls are counted from provider-side observations and cost is recorded as $0.00, not extrapolated to Stripe or other real providers.

Latency distributions (including verification delay) and database-evidence row counts are in summary.json.

Scoring is not frozen. Pending: STOP 2 review and post-review slot calibration.
