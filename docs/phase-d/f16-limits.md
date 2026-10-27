# F16: 429s, long outages and retry storms (M5.4, migration 0018)

**Blueprint:**
- §08 F16: "bounded backoff, limits, and unresolved evidence retained";
- §03: dispatch limits are part of a connector's contract;
- §11 Phase D exit: F14–F16.

## The gap it closes

Before 0018:
- A retryable rejection was reclaimable at once, with nothing bounding how fast claims hit a provider.
- Before its deadline, the reconciler ran a provider lookup on **every pass**: UNKNOWN → VERIFYING → lookup → UNKNOWN, every 0.5 s in the demo. Each pass appended two transitions and one evidence row. Against a provider that is down or answering 429, that is a retry and lookup storm.

## What 0018 does

All of it is **opt-in per contract**, through a row in `safeact_contract_limits`:
- **v1 contracts declare no limits**, so their behaviour is unchanged and every M1–M4 test is untouched.
- **v2 mock contracts** (`core.connector_profiles.LIMITED_PROFILES`) carry the limits agreed in M5 decision 4. The qualification run uses these.
- **The Stripe contract** carries its own limits, below Stripe's documented 25 requests per second.

| Mechanism | Rule | Enforced where |
|---|---|---|
| Retry backoff | Every move to RETRY_ELIGIBLE records `not_before` in `safeact_retry_schedule`, in the same transaction. The delay is `min(cap, base · 2^(attempts−1))` × a jitter fraction derived from md5(operation id, attempt), and never shorter than a `Retry-After` the provider sent (recorded as an attempt fact). | the append function. The claim refuses an early reclaim (SAT30), and the claimable and relay scans leave it out. |
| Attempt budget | unchanged: `max_attempts` bounds the total. RETRY_ELIGIBLE with the budget spent escalates (0008). | claim (SAT13), the reconciler |
| Provider breaker | Per connector and provider account. Consecutive failing dispatch outcomes (no response, server error, rate limited, retryable rejection) reaching the threshold open it for the cooldown. After the cooldown one claim is admitted as the half-open probe. A decisive outcome (SUCCEEDED or FAILED_CONFIRMED) closes it. Key conflicts, in-flight conflicts, a request deadline spent before sending, and lease expiry are not provider failures. | the append function (outcomes) and the claim gate (SAT31). Every open, probe and close is recorded in `safeact_connector_health_events`. |
| Dispatch rate | A token bucket on claims per provider account: `burst`, refilled at `rate` per second. | the claim gate (SAT31) |
| Lookup pacing | UNKNOWN → VERIFYING (one reconciler lookup) is refused before `safeact_next_lookup_at`. That is due at once after a dispatch, then after `interval · 2^(lookups−1)` (capped) since the last lookup, and never later than the investigation deadline, so the last look happens on time and escalation follows. | the append function (SAT30). The reconciler checks `safeact_pacing` first and waits. |
| Ambiguous 429 | A 429 a contract does not document as pre-execution (CRM, email) is `ambiguous_rate_limited`. A documented pre-execution 429 (the payments mock, Stripe) is a retryable rejection with `rate_limited` and `retry_after` facts. | dispatch-outcome vocabulary (0018) |

**A refused claim (SAT30 or SAT31) appends nothing.** The operation stays RECORDED or RETRY_ELIGIBLE, and no claim budget is spent. This is why the limits sit at the claim and not at the dispatch fence: refusing at the fence would force a claim release, and releases count toward the three-abandoned-claims cap, so a storm would escalate everything.

The breaker and token-bucket state lives in PostgreSQL (`safeact_connector_health`), so losing Redis resets nothing. It is the one mutable table besides the projection, and no safety rule depends on it: it only delays work.

## Evidence

All in `tests/integration/test_provider_limits.py`, against PostgreSQL with in-process mocks and the real worker, relay and reconciler, on time-scaled contracts:

| Test | Shows |
|---|---|
| `test_a_rate_limited_dispatch_backs_off` | A 429 leads to RETRY_ELIGIBLE with a recorded `not_before`. The operation is unclaimable and refused (SAT30) until then. The reclaim happens at or after `not_before`. One effect, two provider calls. |
| `test_retry_after_is_never_cut_short` | A `Retry-After` of 1.25 s beats a backoff capped at 0.9 s |
| `test_jitter_is_deterministic_and_bounded` | Same operation and attempt give the same delay, always within `[0, ceiling]`, and the value varies |
| `test_the_breaker_opens_and_admits_one_probe` | Three failing outcomes open the breaker. Claims are refused without any transition. After the cooldown exactly one probe is admitted, and its success closes the breaker. Events: opened, probe_admitted, closed. |
| `test_the_dispatch_rate_bounds_claims` | A burst of 2 admits two claims and refuses the rest. One more is admitted after 0.5 s at 2/s. |
| `test_a_long_outage_paces_lookups_and_escalates_with_evidence` | CRM (no native key) down for longer than the window: about 80 reconciler passes, but at most 7 lookups (the database refuses an early one), every lookup kept as `lookup_unavailable` evidence, escalation at the deadline, one provider call and no effect |
| `test_a_native_key_outage_retries_under_backoff_then_escalates` | A payments outage is retried under the same key, with a recorded backoff each time, until the attempt budget is spent. It then escalates: three calls, no effect. |
| `test_a_crm_rate_limit_is_ambiguous_and_a_provider_failure` | A CRM 429 is `ambiguous_rate_limited` and counts against the breaker |
| `test_a_v1_contract_has_no_limits` | Without a limits row nothing is paced or scheduled, as before |
| `test_a_retry_storm_stays_bounded_and_loses_nothing` | 300 operations, with the first 200 provider calls answered 429. All 300 SUCCEEDED, 500 provider calls in about 31–39 s, within the token-bucket bound. No duplicate effect, every intent accounted for, and no reclaim before its `not_before`. |

Also:
- `test_migration_0018_roundtrip`: round trip, refused downgrade while there is history, autogenerate.
- `tests/phase_a/test_provider_limits_sql.py`: decision-4 values, refused invalid limits, ordering inside the append and claim, the vocabulary.

## Limits (stated, not hidden)

- **Half-open probe:** it is one claim, not one request. A probe whose claim is abandoned frees the slot after the cooldown.
- **Rate limiting:** the bucket counts claims, and each claim makes at most one dispatch. Reconciler lookups have their own pacing, not the bucket.
- **Provider side:** these limits are SafeAct's. A provider can still answer 429, and that is handled as above.
- **Coverage:** 429s and outages are injected on the mocks. They cannot be injected on the Stripe sandbox (see the sandbox contract doc).
- **Contract versions:** v1 mock contracts (`MOCK_PROFILES`, used by the M1–M4 tests) still declare no limits.
- **Demo and E2E (M5.6):** their contracts carry limits at their own pace (`deploy/demo/profiles.PACE_LIMITS`), so the console's next check shows real times.
