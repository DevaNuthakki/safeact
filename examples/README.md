# Integrate a customer-support replacement

This example uses the existing authenticated API to submit one fixed workflow:
charge a shipping fee, create a replacement order, and refund that charge only
if the order is confirmed not created. The API records each effect as a separate
operation. It never turns an empty provider lookup into proof of failure.

Use the [local setup](../README.md#run-it-locally) first. Run commands
from the repository root using the project's virtual environment. The demo
stack must be running on `127.0.0.1:8000`. These examples use synthetic accounts
and mock providers; no Stripe key or model API is needed.

This is a human-session API integration example. The guarded MCP interface
currently exposes individual actions rather than admission of this whole workflow.

## Submit and follow

```sh
.venv/bin/python -m examples.replacement --request-key support-replacement-001
```

Enter the password for `demo-north-requester` from your local
`.demo-credentials` when prompted. The password and session stay in memory and
are never printed or written by the example. The client is restricted to a
loopback HTTP origin, uses no environment proxy, and does not follow redirects.
Explicit cookie forwarding is for the local demo only; the API keeps its
Secure cookie flags.

The default request charges `cust_north_3001` a fee of `500` minor units and
creates `sku-replacement` for `case_north_2001`. Currency is not selected by this
example; for a two-decimal currency, 500 represents 5.00.

The code sends this request to `POST /workflows`:

```json
{
  "request_key": "support-replacement-001",
  "customer_id": "cust_north_3001",
  "case_id": "case_north_2001",
  "sku": "sku-replacement",
  "fee_minor": 500,
  "payments_account_id": "mock-payments-account",
  "crm_account_id": "mock-crm-account",
  "charge_contract": "v1",
  "order_contract": "v1",
  "compensation_contract": "v1"
}
```

The response identifies `workflow_id` and `charge_operation_id`. Poll
`GET /workflows/{workflow_id}` for the derived status and admitted steps; fetch
`GET /actions/{operation_id}` for each step's attempts, evidence, provenance,
contract, and next check. Human sessions use the session cookie plus
`X-SafeAct-CSRF` on writes. The server derives identity and tenant and checks
exact grants; the client cannot select them.

## Handle uncertain responses

The request key identifies one business intent. Retain it in your application
with the exact request body **before** admission. This CLI makes the key explicit
so you can retain and reuse the whole command.

- If admission times out, rerun the **same command with the same key and arguments**.
  A repeated request returns the existing workflow. A changed payload receives
  HTTP 409; resolve the original intent before generating another key.
- A polling timeout means the workflow still needs observation. Follow its
  existing ID or replay the same admission; creating a new key can create a
  separate charge.
- `UNRESOLVED` means an effect may already exist. Inspect evidence in the console;
  never infer that an unknown order needs another order or an immediate refund.
- `COMPENSATION_UNRESOLVED` means the refund is not confirmed. It may be rejected
  or uncertain; inspect the refund operation before taking another action.
- `COMPENSATED_VERIFIED` means evidence confirms a separate refund. It does not
  mean the replacement completed. Open the step to distinguish provider
  verification from reviewer attestation.

Exit codes: `0` for completed replacement or confirmed compensation; `2` for an
API refusal or invalid response; `3` for unresolved, still processing, or a
transport interruption. The JSON status distinguishes success from compensation.
The CLI waits up to 75 seconds by default, configurable with `--wait-seconds`
(0–300). These are observation limits, not cancellation or retry deadlines.

## Exercise the recovery paths

Use a distinct key for each intentionally new workflow:

```sh
# A permanent rejection: confirmed charge, failed order, separate refund.
.venv/bin/python -m examples.replacement --request-key support-refund-001 --case case_north_2004

# Order exists but its response and searchable evidence are unavailable.
.venv/bin/python -m examples.replacement --request-key support-uncertain-001 --case case_north_2002
```

The `2004` case requires a demo bootstrap containing the new grant. Bootstrap
resets demo history and regenerates credentials; avoid running it while following
an existing demo request. The [browser walkthrough](../docs/demo/customer-support.md)
uses the same providers and API.

## Summarize evidence without running the system

```sh
.venv/bin/python -m examples.outcomes docs/phase-e/dev-workflows-20261022/results.jsonl --family F13
.venv/bin/python -m examples.outcomes docs/phase-e/dev-recovery-20261022/results.jsonl
```

These commands only read saved files and print a summary. They make no provider
calls and do not launch qualification. The [outcomes guide](../docs/demo/outcomes.md)
explains denominators and the remaining comparison work.
