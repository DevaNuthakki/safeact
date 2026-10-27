# SafeAct qualification evidence

diagnostic

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
| b0 | 0/46 | 11 | 0 | 0 | 71 | 25 | 0 | 72 |
| b1 | 11/118 | 107 | 0 | 0 | 225 | 107 | 0 | 0 |
| b2 | 67/118 | 51 | 0 | 0 | 169 | 51 | 0 | 0 |
| b3 | 118/118 | 0 | 0 | 0 | 118 | 0 | 0 | 0 |
| safeact | 118/118 | 0 | 0 | 0 | 118 | 0 | 0 | 0 |

## By family

| Group | Useful / achievable | Safety-failure rows | Error rows | Unstable at scoring | Effects | Duplicates | Orphans | N/A rows |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| F04 | 8/10 | 2 | 0 | 0 | 14 | 4 | 0 | 0 |
| F04x8 | 8/10 | 2 | 0 | 0 | 24 | 14 | 0 | 0 |
| F05 | 85/175 | 55 | 0 | 0 | 230 | 55 | 0 | 0 |
| F05+F08 | 25/44 | 19 | 0 | 0 | 63 | 19 | 0 | 11 |
| F06 | 128/196 | 68 | 0 | 0 | 264 | 68 | 0 | 49 |
| F08 | 32/48 | 16 | 0 | 0 | 64 | 16 | 0 | 12 |
| F10 | 28/35 | 7 | 0 | 0 | 42 | 7 | 0 | 0 |

## By connector

| Group | Useful / achievable | Safety-failure rows | Error rows | Unstable at scoring | Effects | Duplicates | Orphans | N/A rows |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| crm | 122/249 | 107 | 0 | 0 | 363 | 114 | 0 | 31 |
| email | 8/10 | 2 | 0 | 0 | 13 | 3 | 0 | 0 |
| payments | 184/259 | 60 | 0 | 0 | 325 | 66 | 0 | 41 |

## Outcomes by arm

- b0: {"confirmed_success": 11, "pending": 35}
- b1: {"confirmed_success": 118}
- b2: {"confirmed_success": 118}
- b3: {"confirmed_success": 118}
- safeact: {"confirmed_success": 118}

Effect attribution by arm:

- b0: 71 forward, 0 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.
- b1: 225 forward, 0 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.
- b2: 169 forward, 0 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.
- b3: 118 forward, 0 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.
- safeact: 118 forward, 0 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.

Provider activity and cost basis:

- b0: 71 mock calls (71 dispatch, 0 lookup); external provider cost $0.00.
- b1: 225 mock calls (225 dispatch, 0 lookup); external provider cost $0.00.
- b2: 248 mock calls (248 dispatch, 0 lookup); external provider cost $0.00.
- b3: 319 mock calls (161 dispatch, 158 lookup); external provider cost $0.00.
- safeact: 409 mock calls (173 dispatch, 236 lookup); external provider cost $0.00.

Mock qualification providers have no external charge; provider calls are counted from provider-side observations and cost is recorded as $0.00, not extrapolated to Stripe or other real providers.

Latency distributions (including verification delay) and database-evidence row counts are in summary.json.

Scoring is frozen to the verified input manifest.
