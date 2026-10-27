# Stripe sandbox adapter: contract, key handling, run plan (M5.3)

**Status (2026-10-22):** M5.8 completed. The saved offline and real test-mode
runs each passed 16/16 trials; the real run used 77/150 calls. See the
[M5.8 evidence review](stripe-sandbox-v1/REVIEW-M58.md) for results, hashes and
limits. The key setup and approval instructions below describe the original
STOP 1 procedure; they do not request another run.

**Blueprint:**
- §02 "required external qualification";
- §03 "payments: … exercise a provider retention boundary and a real sandbox adapter";
- §13 "mocks and real sandbox behaviour are identified separately".

## What was built

| Piece | Location |
|---|---|
| Contract `stripe-test` v1 (and `v1-run`, shorter windows for the run) | `core/connector_profiles.py` (`stripe_profile`, `STRIPE_API_VERSION`) |
| Native-key registration (server-derived `sa1_` key) | `core/provider_keys.py` |
| Key loader and sandbox gate | `connectors/stripe_key.py` |
| Adapter (raw HTTPS through `httpx2`, no SDK) | `connectors/stripe_payments.py` |
| Fake Stripe: documented behaviour only, offline, with an independent ledger | `connectors/stripe_fake.py` |
| Sandbox runner and frozen manifest | `deploy/sandbox/run_stripe.py`, `deploy/sandbox/manifest_stripe_v1.json` |
| Tests (no network) | `tests/phase_a/test_stripe_adapter.py`, `test_stripe_sandbox_manifest.py`, `tests/integration/test_stripe_sandbox_dry_run.py` |

## The contract

The adapter pins `Stripe-Version: 2026-09-30.endive` on every request, never the account default. Sources are Stripe's docs, read 2026-10-20:
- [idempotent requests](https://docs.stripe.com/api/idempotent_requests)
- [advanced error handling](https://docs.stripe.com/error-low-level)
- [rate limits](https://docs.stripe.com/rate-limits)
- [refunds](https://docs.stripe.com/api/refunds)
- [keys](https://docs.stripe.com/keys)
- [upgrades](https://docs.stripe.com/upgrades)

| Capability | What Stripe documents | What the contract assumes |
|---|---|---|
| Key scope | per account | `provider_account` |
| Retention | "Stripe retains keys for at least 24 hours and can remove them after that" | 24 h, less a 1 h safety margin. There is no protected retry once `first fence + 24 h − 1 h` has passed (F09). |
| Parameter binding | "compares incoming parameters to those of the original request and errors if they're not the same" | binds parameters. A mismatch is `ambiguous_key_conflict`, never the earlier result (F10). |
| Concurrent same key | `409` "another request with the same key is still executing"; not saved | protects concurrent requests. `409` is `provider_in_flight_conflict` (F04). |
| Saved results | "saves … after endpoint execution begins … including 500 errors"; validation, auth and rate-limit failures are not saved | a replay never re-executes. A `500` is indeterminate. |
| Lookup | list refunds (newest first, `created` filter, cursor pagination); no consistency promise | **EVENTUAL**: absence is never proof of non-application (F07). |
| Compensation | a refund cannot be refunded | none |

**Dispatch** is `POST /v1/refunds`, form-encoded, with:
- `payment_intent` and `amount`;
- `metadata[safeact_operation_id]` and `metadata[safeact_payload_sha256]`;
- the `Idempotency-Key` header;
- `Authorization: Bearer <key>`.

There are no client retries and no redirects. The timeout is the remaining request deadline.

**Lookup** lists refunds created since the first dispatch minus 5 minutes, at most 10 pages of 100, and matches `metadata[safeact_operation_id]`. The match counts only if the recorded payload hash equals this operation's. Reading more pages than that, a non-200 answer, or a malformed body counts as unavailable (inconclusive).

**What APPLIED means here:** a refund object exists.
- **Status is recorded, not judged.** The refund's `status` as Stripe reported it (`pending`, `requires_action`, `succeeded`, `failed` or `canceled`) is stored as evidence detail: `details.provider.refund_status` on the attempt outcome, from the create response or from the lookup that found it. It is a non-decisive provider fact; no decision reads it (a structural test enforces this).
- **Asynchronous failure is out of scope.** A refund that is created and later fails asynchronously is outside what SafeAct verifies. SafeAct verifies creation, not settlement, and it has no webhook handling: Stripe's `refund.failed` and `refund.updated` events are not received. Such a refund stays SUCCEEDED in SafeAct, with the status recorded at the time it was seen. This is stated in the README limits too.

### Classification

Anything not listed is AMBIGUOUS. A status code alone is never treated as proof (§04).

| Stripe answer | SafeAct outcome | Why |
|---|---|---|
| `200` refund object | APPLIED (reference = refund id) | the effect exists |
| `401`, `403` | NOT_APPLIED | authentication and permission fail before execution |
| `400 idempotency_error` | AMBIGUOUS `ambiguous_key_conflict` | same key, other parameters |
| `400`/`402` with `parameter_missing`, `parameter_invalid_*`, `parameter_unknown`, `resource_missing`, `amount_too_small` | NOT_APPLIED | validation before execution, nothing created |
| `400`/`402` with `charge_already_refunded`, `amount_too_large` | NOT_APPLIED | Stripe raises these instead of creating the refund |
| `409` | AMBIGUOUS `provider_in_flight_conflict` | the same key is still executing |
| `429` with `Stripe-Rate-Limited-Reason`, or code `lock_timeout` | NOT_APPLIED, retryable | "Stripe doesn't process these failed requests". The retry goes through the protected-retry rules and M5.4's durable backoff. |
| `429` without either | AMBIGUOUS | Stripe says it was not a rate limit |
| any `5xx`, an unknown status, a malformed `200` | AMBIGUOUS | a `500` may follow an applied effect (F03) |
| timeout, reset, broken connection | AMBIGUOUS `ambiguous_no_response` | the effect is unknown (F01) |
| malformed payload (rejected locally) | NOT_APPLIED `local:invalid_payload` | nothing was sent |
| any object with `livemode: true` | **stop** (`LiveModeRefused`) | test mode only |

## Key handling

- **The file:** you create it yourself, at `~/.config/safeact/stripe.key` (or `SAFEACT_STRIPE_KEY_FILE`), mode 600, outside the repository. A symbolic link, a file not owned by you, or any group or other permission bit is refused.
- **Accepted keys:** only a restricted test-mode key (`rk_test_`). These are refused by prefix before anything is sent:
  - `sk_live_`, `rk_live_`, `pk_live_`;
  - `sk_test_` (you asked for a restricted key);
  - `pk_test_`, `whsec_`, `sk_org_`.

  No refusal message contains the key or any part of it beyond its kind.
- **In memory and on the wire:**
  - the value lives only in `StripeKey`, whose `repr`/`str` hide it and which cannot be pickled;
  - it is revealed only to build the `Authorization` header;
  - it is never in a URL or a body (tested).
- **Network gate:** a real network client exists only with `SAFEACT_ALLOW_STRIPE_SANDBOX=1`, never when `CI` or `GITHUB_ACTIONS` is set, and only to `https://api.stripe.com`. The gate is checked before the runner creates any file. **CI never calls Stripe:** it runs the same runner with `--fake`.
- **Leak checks:**
  - application-log redaction and the secret scan now catch `sk_`/`rk_`/`pk_` + `test_`/`live_`, and `whsec_`;
  - the runner scans its own output and refuses a run that contains anything secret-shaped;
  - the call log keeps a 16-character tag of each `sa1_` key, never the key.
- **Deviation from the plan:** the isolated PostgreSQL log tests got no `rk_test_` control. The Stripe key never reaches PostgreSQL (nothing stores or sends it there), so a database-log control would prove nothing. Application-log redaction is tested in Phase A instead.

## What to create (STOP 1)

1. **Account:** a Stripe account used in **test mode / a sandbox only**. Live payments are never activated.
2. **Key:** in that sandbox, a **restricted key** (`rk_test_…`) with exactly these permissions:

   | Resource | Permission | Used for |
   |---|---|---|
   | PaymentIntents | Write | the fixture: a confirmed test PaymentIntent with `pm_card_visa` per scenario |
   | Refunds | Write | the refunds under test (Write includes reading refunds for the lookup) |
   | Charges | Read | as agreed. Not called by the current code; kept for the observer if Stripe requires it for refund listing |

   Everything else: **None**.
3. **Save it:** write the key to `~/.config/safeact/stripe.key`, then run `chmod 600 ~/.config/safeact/stripe.key`. Claude never reads, prints or logs that file.

## The sandbox run (M5.8, after STOP 1)

```
SAFEACT_ALLOW_STRIPE_SANDBOX=1 .venv/bin/python -m deploy.sandbox.run_stripe \
    --manifest deploy/sandbox/manifest_stripe_v1.json --out docs/phase-d/stripe-sandbox-v1/run-1
```

- **Scenarios:** 8 frozen scenarios × 2 repeats, with a budget of at most 150 calls (the offline dry run uses 60). The Stripe sandbox is free; cost is recorded as $0.

| Id | Path | Observed against Stripe's own record |
|---|---|---|
| ST-01 | through SafeAct | a refund: SUCCEEDED, one refund, one POST, and the refund status recorded as evidence |
| ST-02 | through SafeAct | timeout after success: Stripe applies the refund, the harness drops the response. SafeAct goes UNKNOWN → lookup → SUCCEEDED, still one refund and one POST. The status is recorded from the lookup. |
| ST-03 | direct | same key, same parameters: the same refund id, `Idempotent-Replayed: true`, one refund |
| ST-04 | direct | same key, other parameters: `idempotency_error`, one refund |
| ST-05 | direct | the same key sent twice at once: one refund (statuses recorded) |
| ST-06 | through SafeAct | a refund larger than the payment: no refund. FAILED_CONFIRMED if Stripe returns a documented code, otherwise ESCALATED_UNRESOLVED (conservative). |
| ST-07 | through SafeAct | a second full refund: still one refund. The second operation ends FAILED_CONFIRMED or ESCALATED_UNRESOLVED (as ST-06). |
| ST-08 | direct | how soon a new refund is listed (lookup visibility), recorded |

- **Ground truth:** after every scenario, the refunds Stripe lists for that PaymentIntent, read separately. It is never SafeAct's view.
- **Recorded:** Stripe request ids, latency, replay headers, the pinned version, hardware and timings.
- **Reporting:** every difference from the frozen expectation is reported, never edited away.

## Known limits (stated, not hidden)

- **Faults:** no 429, 5xx or retention expiry can be injected at Stripe. Those paths are proven against the fake and the mocks (F16 on mocks, M5.4), and the report says so.
- **Timeout after success:** produced by dropping a response that Stripe really applied, not by a real network failure.
- **Lookup:** relies on our own metadata, and on refunds being listable within the window. Consistency is not documented, so absence is never proof.
- **Settlement:** a refund created and later failing asynchronously is not detected (no webhooks). SafeAct claims creation only and records the status it saw.
- **Lifetime:** the API version and these behaviours are as documented on 2026-10-20. A new `Stripe-Version` needs a new contract version and a fresh read of the docs.
