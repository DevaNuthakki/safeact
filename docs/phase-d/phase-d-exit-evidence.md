# Phase D evidence: API, console, agent boundary and Stripe sandbox

Status reviewed on 2026-10-22. M5.1–M5.8 are committed through `3269385`.
The Phase E qualification harness (M5.9) has additional uncommitted work.
This document collects the existing Phase D evidence; it does not claim that
Phase E qualification or production readiness is complete.

## Evidence by requirement

| Requirement | Evidence | Scope |
|---|---|---|
| Authenticated API, human review, console journeys and F14 | [Milestone 3 exit](milestone-3-exit-evidence.md), [API authorization review](api-authorization-review.md) | Local synthetic accounts and mock providers; stale decisions are refused. |
| Guarded MCP tools, scoped agent identity, no self-approval and F15 | [Milestone 4 exit](milestone-4-exit-evidence.md), [MCP authorization review](mcp-authorization-review.md) | Agent identity and grants are checked in PostgreSQL; approvals remain human-only. |
| Agent evaluation, A9 agent portion | [Agent results and disclosures](agent-eval-v1/README.md) | One model snapshot, mock providers, 72 held-out trials per arm; observed counts, not estimated rates. |
| Real provider sandbox, A9 sandbox portion | [Stripe contract](stripe-sandbox-contract.md), [M5.8 evidence review](stripe-sandbox-v1/REVIEW-M58.md) | Stripe test mode; 8 scenarios × 2 repeats. Some scenarios drive SafeAct's durable worker/reconciler, others observe the adapter directly. |
| F16: backoff, rate limits, breaker, lookup pacing and retained uncertainty | [F16 design and tests](f16-limits.md), `tests/integration/test_provider_limits.py` | Faults injected on mocks with time-scaled contracts; no rate-limit or outage injection against Stripe. |
| Real worker death | `tests/integration/test_worker_process_kill.py` | Separate prefork processes, actual SIGKILL, provider-effect checks and projection-drift checks. |
| Truthful next-check display and metrics | [Observability](../phase-e/observability.md), `tests/integration/test_next_check.py`, `ui/e2e/next-check.spec.ts` | Next check is a not-before time; metrics use bounded labels. |

## Saved Stripe result

The recorded real run began on 2026-10-21 and used 77 of its 150-call budget.
All 16 trials passed with no DIFFERENT or ERROR result. Its offline counterpart
also passed 16/16. The [review](stripe-sandbox-v1/REVIEW-M58.md) identifies the
run's base commit, manifest, artifact hashes and working-tree context.

Observed cases include refund creation, a lost response after creation,
same-key replay, changed parameters, concurrent requests, unresolved ambiguity,
rejection after a charge was already refunded, and refund-list visibility.
The unresolved case is a safety result, not successful completion.

"Applied" means a refund object exists. Refund status is retained as
non-decisive evidence; settlement, later asynchronous failure and webhooks are
outside this experiment. Retention expiry and provider outages are tested
with mocks, not demonstrated against real Stripe.

No credential contents were inspected for this status review, and no new
Stripe request was needed. The restricted-test-key loader, explicit sandbox
gate and CI refusal remain in place.

## Remaining work and boundaries

Phase E still needs complete harness coverage and scoring, B0–B3 baselines,
the frozen corpus and STOP 2 review, the full qualification run, and load-test
results. Compose packaging, the fresh-clone check, demo walkthrough and final
evidence pack follow those steps. Existing development results do not replace
these requirements.

Production execution remains deny-only. Worker-side identity binding and
other limits disclosed in the earlier exit evidence remain open. The
[observations log](../phase-e/observations.md) retains the macOS fork caveat
and past startup-race evidence.
