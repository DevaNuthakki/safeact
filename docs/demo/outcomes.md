# Measured customer outcomes

These are retained development experiments using mock providers. They are small
samples with time-scaled contracts. Full qualification, B0–B3 comparisons and
load results are pending; there is no measured claim about production savings
or comparative overhead.

| Evidence packet | Requested units | Useful / declared achievable | Unresolved outcomes | Purpose |
|---|---:|---:|---:|---|
| F13 workflows only | 5 workflows | 2/2 | 3 | Distinguish completed replacement, confirmed compensation, and uncertainty |
| Recovery development | 29 operations | 14/22 | 8 | Preserve timeout, retention, outage and retry-storm results |

The F13 sample observed one completed replacement and one confirmed
compensation. The other three workflows stayed unresolved. Nine actual effects
were recorded: seven forward effects and two refund effects. One refund really
occurred but remained hidden from the application, so it was correctly not
claimed as verified compensation. No prohibited forward duplicate, duplicate
refund, invalid refund, orphan effect or false outcome claim was observed.

The sample required 36 mock provider calls: 15 dispatch and 21 lookup. Resolution
latency p50 was 0.517 seconds and p95 2.052 seconds across the two measured
resolutions. These figures exclude unresolved workflows; they are not workload
latency or a comparison with unguarded execution. Provider cost was $0 for mocks,
not a Stripe pricing estimate.

Read the generated summaries, which can be reproduced using
[`examples.outcomes`](../../examples/outcomes.py):

- [Workflow outcomes](workflow-outcomes.md), generated from the five F13 rows in
  the [original results](../phase-e/dev-workflows-20261022/results.jsonl).
- [Recovery outcomes](recovery-outcomes.md), generated from the
  [original recovery results](../phase-e/dev-recovery-20261022/results.jsonl).
- [Workflow evidence review](../phase-e/dev-workflows-20261022/REVIEW.md) and
  [recovery evidence review](../phase-e/dev-recovery-20261022/REVIEW.md), including
  raw traces, source snapshots, retained failures and review limits.

Useful completion counts only a safe completed or compensated unit declared
achievable. Unresolved work never becomes completion. Refused and errored cases
stay in the achievable denominator. N/A rows are disclosed separately. Counts of
unresolved outcomes indicate investigation is needed; they do not measure how
many human reviews occurred. Calls show provider activity; additional-call
overhead requires a comparable baseline, which is still pending.

The original F16 storm completed only 1/8 achievable operations. The saved
recovery packet also includes missed achievable completion during payment
outages. These are open findings, even though no prohibited duplicates were
observed. Safety and useful completion must be assessed separately.
The recovery packet also has seven confirmed failures (non-application), shown
separately in its generated summary; they do not disappear from requested units.

The [earlier held-out agent experiment](../phase-d/agent-eval-v1/README.md)
compares guarded model tools, direct model tools and a deterministic caller.
Its scope and trials differ from these workflow experiments; do not merge its
counts into this denominator or label it the pending B0–B3 system qualification.
