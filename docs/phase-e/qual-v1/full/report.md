# SafeAct qualification evidence

full

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
| b0 | 149/438 | 250 | 0 | 0 | 986 | 371 | 0 | 556 |
| b1 | 426/691 | 239 | 0 | 0 | 1115 | 213 | 0 | 294 |
| b2 | 558/691 | 92 | 0 | 0 | 949 | 92 | 0 | 294 |
| b3 | 841/874 | 0 | 0 | 16 | 1078 | 0 | 0 | 63 |
| safeact | 924/937 | 0 | 0 | 0 | 1180 | 0 | 0 | 0 |

## By family

| Group | Useful / achievable | Safety-failure rows | Error rows | Unstable at scoring | Effects | Duplicates | Orphans | N/A rows |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| CONTROL | 300/300 | 0 | 0 | 0 | 300 | 0 | 0 | 0 |
| F01 | 141/255 | 114 | 0 | 0 | 435 | 115 | 0 | 0 |
| F02 | 320/320 | 0 | 0 | 0 | 155 | 0 | 0 | 0 |
| F03 | 112/215 | 103 | 0 | 0 | 423 | 103 | 0 | 0 |
| F04 | 256/297 | 41 | 0 | 0 | 406 | 109 | 0 | 23 |
| F04x8 | 80/93 | 13 | 0 | 0 | 184 | 91 | 0 | 7 |
| F05 | 121/220 | 55 | 0 | 0 | 322 | 55 | 0 | 0 |
| F05+F08 | 53/72 | 19 | 0 | 0 | 95 | 19 | 0 | 20 |
| F06 | 188/256 | 68 | 0 | 0 | 324 | 68 | 0 | 64 |
| F06+F07 | 40/40 | 0 | 0 | 0 | 40 | 0 | 0 | 60 |
| F07 | 128/128 | 0 | 0 | 0 | 128 | 0 | 0 | 192 |
| F08 | 226/256 | 16 | 0 | 0 | 258 | 16 | 0 | 64 |
| F09 | 0/0 | 0 | 0 | 2 | 185 | 0 | 0 | 0 |
| F09+F03 | 0/0 | 0 | 0 | 0 | 60 | 0 | 0 | 0 |
| F10 | 256/320 | 64 | 0 | 0 | 384 | 64 | 0 | 0 |
| F11 | 198/208 | 0 | 0 | 1 | 212 | 0 | 0 | 64 |
| F12 | 54/54 | 0 | 0 | 3 | 128 | 0 | 0 | 192 |
| F13 | 99/125 | 88 | 0 | 2 | 679 | 36 | 0 | 0 |
| F13+F12 | 18/18 | 0 | 0 | 0 | 80 | 0 | 0 | 60 |
| F14 | 126/126 | 0 | 0 | 0 | 126 | 0 | 0 | 189 |
| F15 | 63/63 | 0 | 0 | 0 | 63 | 0 | 0 | 252 |
| F16 | 85/165 | 0 | 0 | 5 | 226 | 0 | 0 | 0 |
| F16+F11 | 34/100 | 0 | 0 | 3 | 95 | 0 | 0 | 20 |

## By connector

| Group | Useful / achievable | Safety-failure rows | Error rows | Unstable at scoring | Effects | Duplicates | Orphans | N/A rows |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| crm | 968/1214 | 226 | 0 | 12 | 1656 | 272 | 0 | 751 |
| email | 575/638 | 39 | 0 | 0 | 808 | 77 | 0 | 62 |
| payments | 1355/1779 | 316 | 0 | 4 | 2844 | 327 | 0 | 394 |

## Outcomes by arm

- b0: {"confirmed_failure": 33, "confirmed_success": 360, "pending": 430, "verified_compensation": 13}
- b1: {"confirmed_failure": 33, "confirmed_success": 683, "pending": 458, "verified_compensation": 13}
- b2: {"confirmed_failure": 33, "confirmed_success": 677, "pending": 464, "verified_compensation": 13}
- b3: {"confirmed_failure": 127, "confirmed_success": 809, "escalated_unresolved": 447, "pending": 13, "verified_compensation": 22}
- safeact: {"confirmed_failure": 67, "confirmed_success": 911, "escalated_unresolved": 468, "pending": 13, "verified_compensation": 22}

Effect attribution by arm:

- b0: 921 forward, 65 compensation, 0 orphan; 39 duplicate compensation effects and 0 invalid compensation effects.
- b1: 1050 forward, 65 compensation, 0 orphan; 39 duplicate compensation effects and 0 invalid compensation effects.
- b2: 923 forward, 26 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.
- b3: 1032 forward, 46 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.
- safeact: 1134 forward, 46 compensation, 0 orphan; 0 duplicate compensation effects and 0 invalid compensation effects.

Provider activity and cost basis:

- b0: 1780 mock calls (1780 dispatch, 0 lookup); external provider cost $0.00.
- b1: 1923 mock calls (1923 dispatch, 0 lookup); external provider cost $0.00.
- b2: 1940 mock calls (1940 dispatch, 0 lookup); external provider cost $0.00.
- b3: 6915 mock calls (2074 dispatch, 4841 lookup); external provider cost $0.00.
- safeact: 8255 mock calls (2250 dispatch, 6005 lookup); external provider cost $0.00.

Mock qualification providers have no external charge; provider calls are counted from provider-side observations and cost is recorded as $0.00, not extrapolated to Stripe or other real providers.

Latency distributions (including verification delay) and database-evidence row counts are in summary.json.

Scoring is frozen to the verified input manifest.
