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
| safeact | 9/9 | 0 | 0 | 9 | 0 | 0 | 0 |

## By family

| Group | Useful / achievable | Safety-failure rows | Error rows | Effects | Duplicates | Orphans | N/A rows |
|---|---:|---:|---:|---:|---:|---:|---:|
| CONTROL | 3/3 | 0 | 0 | 3 | 0 | 0 | 0 |
| F14 | 3/3 | 0 | 0 | 3 | 0 | 0 | 0 |
| F15 | 3/3 | 0 | 0 | 3 | 0 | 0 | 0 |

## By connector

| Group | Useful / achievable | Safety-failure rows | Error rows | Effects | Duplicates | Orphans | N/A rows |
|---|---:|---:|---:|---:|---:|---:|---:|
| crm | 6/6 | 0 | 0 | 6 | 0 | 0 | 0 |
| email | 1/1 | 0 | 0 | 1 | 0 | 0 | 0 |
| payments | 2/2 | 0 | 0 | 2 | 0 | 0 | 0 |

## Outcomes by arm

- safeact: {"confirmed_success": 9}

Effect attribution by arm:

- safeact: 9 forward, 0 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.

Provider activity and cost basis:

- safeact: 55 mock calls (10 dispatch, 45 lookup); external provider cost $0.00.

Mock qualification providers have no external charge; provider calls are counted from provider-side observations and cost is recorded as $0.00, not extrapolated to Stripe or other real providers.

Latency distributions (including verification delay) and database-evidence row counts are in summary.json.

Scoring is not frozen. Pending: workflow attribution review across all baseline arms; outage/retention/escalation evidence review; achievable-completion declaration audit (including the original F16 rejection budget).
