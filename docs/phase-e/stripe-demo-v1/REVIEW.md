# Stripe test-mode demo profile: real run (2026-10-25)

> **Note (2026-10-26, added before publishing; the record below is unchanged):**
> "Succeeded" or "applied" for a Stripe refund means Stripe accepted the request and
> created the refund object. The refund's own status is the value Stripe reported in
> its response at that moment (`details.provider.refund_status`), not a live value.
> Settlement to the customer, and later asynchronous failure, are out of scope.

The optional demo profile (`deploy/demo/stripe_demo.py`) was run once against Stripe
**test mode**, with the author's go, following
[the walkthrough's Stripe section](../../demo-walkthrough.md#optional-the-same-refund-against-stripe-test-mode).
It sent one normal refund, and one refund whose response the demo harness drops after
Stripe applied it. The full record is in [results.json](results.json).

| | |
|---|---|
| Commands | `SAFEACT_ALLOW_STRIPE_SANDBOX=1 python -m deploy.demo.stripe_demo setup`, `python -m deploy.demo.bootstrap --stripe`, `SAFEACT_ALLOW_STRIPE_SANDBOX=1 python -m deploy.demo.run_stack --stripe`, then the two refunds through `POST /actions` as `demo-north-requester` (the console's call), then `stripe_demo check` |
| Stripe | test mode, `Stripe-Version: 2026-09-30.endive`, restricted `rk_test_` key loaded by the process from `~/.config/safeact/stripe.key` (Claude never reads, prints or logs it) |
| Amounts | each PaymentIntent 20.00 USD; each refund 5.00 USD |
| Calls | 7 in all: 2 PaymentIntent creations, **2 refund POSTs (one per operation)**, 1 reconciler lookup, 2 refund listings by `check` |
| Cost | $0 (test mode) |

## Result

| Case | PaymentIntent | Operation | SafeAct path | Attempts | Refund POST (Stripe request id) | Refunds at Stripe |
|---|---|---|---|---:|---|---:|
| normal | `pi_3UUbA1DnpP0MDY8U4RYP3jpm` | `f8f47ac1-dcf3-4633-b178-19fa1350471c` | CLAIMED → DISPATCHING → SUCCEEDED (`provider_confirmed`) | 1 | `req_QbeYYZ6EmdU39C` (200) | **1** (`re_3UUbA1DnpP0MDY8U4MnuTsYQ`, 500, succeeded) |
| response_lost | `pi_3UUbA2DnpP0MDY8U0R6eGrx5` | `497a777c-c90b-43ec-b729-1e477776de16` | CLAIMED → DISPATCHING → UNKNOWN (`ambiguous_no_response`) → VERIFYING → SUCCEEDED (`provider_effect_confirmed`) | 1 | `req_BiLvrB1WPbI0PE` (200; response then dropped by the harness) | **1** (`re_3UUbA2DnpP0MDY8U0uNzzhJ3`, 500, succeeded) |

**The dropped-response case:**
- **What happened:** Stripe answered 200 and created the refund. The harness then
  discarded that answer, so SafeAct saw no response and recorded UNKNOWN.
- **How it was verified:** the reconciler listed refunds (`req_B3lA7qE8N5uAg5`), found
  this operation's refund by its metadata, and moved the operation to SUCCEEDED with
  provider-verified provenance.
- **No resend:** no second POST was made. The call log has exactly one refund POST for
  this operation's key tag (`592f86aa62cee4c3`), and Stripe lists exactly one refund
  for the PaymentIntent.

`stripe_demo check` reported: **PASS: exactly one refund per PaymentIntent**.

**Independent check by the author (2026-10-26):** in the Stripe test dashboard,
`pi_3UUbA2DnpP0MDY8U0R6eGrx5` (the dropped-response case) shows exactly one refund
event, US$5.00, matching this record.

Other Stripe request ids:
- PaymentIntent creation: `req_8ZF5DiGWqyWx93` (normal) and `req_yRZayfSaWnA6Qw`
  (response_lost).
- The `check` listings: `req_t5Mk07fIslVJPC` and `req_1QaKDNUOzpKf4D`.

## What was recorded, and what never was

- **The call log** (`.demo-stripe-calls.jsonl`, mode 600, gitignored; copied into
  `results.json`) keeps per call: the method, path, status, Stripe `Request-Id`,
  `Idempotent-Replayed`, latency, and a 16-character tag of the derived idempotency
  key.
- **Never recorded:** the API key, the full idempotency keys, any other header, and any
  body.
- **Mapping:** the tags were matched to operations from the database without printing
  any key.
- **Secret scan:** the packet passed it.

## Limits

- **One run, two cases:** the profile's purpose is the demo, not a qualification.
  Stripe-specific qualification is the [M5.8 sandbox run](../../phase-d/stripe-sandbox-v1/REVIEW-M58.md)
  (16/16).
- **The dropped response is staged:** it is harness behaviour, fixed by the fixture
  file, not a network fault.
- **"Applied" means a refund object exists.** Both refunds reported `succeeded`; a
  later asynchronous failure is outside what SafeAct verifies (no webhooks).
- **Shutdown exit code (explained, not a defect):** the background job reported status 1
  after the stack was stopped with `pkill -INT -f "deploy.demo.run_stack --stripe"`.
  - **Cause:** that pattern also matched the shell that had launched the job (its
    command line contains the same text), so SIGINT interrupted the shell itself, and
    status 1 is that interrupted shell's.
  - **Reproduced** on the mock stack: the same stop gave status 1, and the stack's own
    exit status was never even recorded, because the shell was interrupted first.
  - **The stack itself:** a direct SIGINT to its process exits 0, and its log shows no
    error.
  - **Lesson:** stop it by PID. No live-mode object was observed during the run.
