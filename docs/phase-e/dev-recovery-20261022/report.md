# SafeAct development evidence

development only; qualification incomplete

Safety failures and useful completion are reported separately. A zero exit status checks
harness validity and SafeAct safety; it does not assert useful completion or full coverage.

Useful completion counts requested operations whose declared acceptable terminal and
independent effect count agree, divided by all operations declared achievable.

Refusals and errored cases stay in that denominator. N/A rows are excluded and counted separately.

The final ledger includes effects drained during cleanup. Coverage and completion use the
observation window; cleanup releases do not establish a scheduled interleaving.

## By arm

| Group | Useful / achievable | Safety-failure rows | Error rows | Effects | Duplicates | Orphans | N/A rows |
|---|---:|---:|---:|---:|---:|---:|---:|
| safeact | 14/22 | 0 | 0 | 17 | 0 | 0 | 0 |

## By family

| Group | Useful / achievable | Safety-failure rows | Error rows | Effects | Duplicates | Orphans | N/A rows |
|---|---:|---:|---:|---:|---:|---:|---:|
| CONTROL | 3/3 | 0 | 0 | 3 | 0 | 0 | 0 |
| F07 | 5/5 | 0 | 0 | 5 | 0 | 0 | 0 |
| F09 | 0/0 | 0 | 0 | 3 | 0 | 0 | 0 |
| F16 | 6/14 | 0 | 0 | 6 | 0 | 0 | 0 |

## By connector

| Group | Useful / achievable | Safety-failure rows | Error rows | Effects | Duplicates | Orphans | N/A rows |
|---|---:|---:|---:|---:|---:|---:|---:|
| crm | 2/2 | 0 | 0 | 2 | 0 | 0 | 0 |
| email | 1/1 | 0 | 0 | 1 | 0 | 0 | 0 |
| payments | 11/19 | 0 | 0 | 14 | 0 | 0 | 0 |

## Outcomes by arm

- safeact: {"confirmed_failure": 7, "confirmed_success": 14, "escalated_unresolved": 8}

Provider activity and cost basis:

- safeact: 220 mock calls (63 dispatch, 157 lookup); external provider cost $0.00.

Mock qualification providers have no external charge; provider calls are counted from provider-side observations and cost is recorded as $0.00, not extrapolated to Stripe or other real providers.

Latency distributions (including verification delay) and database-evidence row counts are in summary.json.

Scoring is not frozen. Pending: workflow and compensation effect attribution; outage/retention/escalation evidence review; achievable-completion declaration audit (including the original F16 rejection budget).
