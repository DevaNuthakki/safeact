# Customer outcomes — development evidence

Mock providers; small development sample; scoring is not frozen. Full qualification and B0–B3 comparisons are NOT_RUN.

A workflow counts as one requested unit. A confirmed refund is a recovery outcome, not a completed replacement.

| Arm | Requested units | Useful / achievable | Confirmed success | Confirmed compensation | Unresolved | Error rows |
|---|---:|---:|---:|---:|---:|---:|---:|
| safeact | 5 | 2/2 | 1 | 1 | 3 | 0 |

safeact: 0 prohibited forward duplicates, 0 duplicate refunds, 0 invalid refunds, 0 orphan effects and 0 false outcome claims observed.

Provider activity: 36 calls (15 dispatch, 21 lookup). Resolution latency: p50 0.517 s, p95 2.052 s across 2 measured units (0 invalid measurements).

N/A rows: 0; excluded from requested units and completion counts.

Unresolved outcomes indicate investigation is needed; these are not counts of human reviews performed.

Useful completion includes confirmed compensation where declared achievable. Refusals and errors stay in the achievable denominator.

Latency covers measured resolutions only, not every request or the time spent unresolved; it is not full workload latency.

Provider calls show activity, not comparative overhead: no baseline is available yet. No estimate of money saved or duplicates prevented is made.

Mock qualification providers have no external charge; provider calls are counted from provider-side observations and cost is recorded as $0.00, not extrapolated to Stripe or other real providers.
