# Customer-support walkthrough

SafeAct's initial use case is a support agent requesting a replacement with a
shipping fee. A network timeout can leave the business uncertain about a charge,
order, or refund. The demo shows how recorded evidence governs the next action.

## Start the local demo

Follow the root [setup instructions](../../README.md#run-it-locally).
Start the API/worker stack and the console in separate terminals. Sign in at
`http://127.0.0.1:5173` as `demo-north-requester` using your local generated
password. All providers and resources are synthetic. No real payment or model
API call is made.

This walkthrough admits the fixed workflow through the human console/API.
The existing guarded MCP interface supports individual refund, replacement-order
and email actions; it does not currently expose this whole workflow as an
agent tool.

Existing installations need a demo rebuild to apply the admission fixes and
add `case_north_2004`:

```sh
.venv/bin/python -m deploy.demo.bootstrap
```

This resets `safeact_demo` and regenerates credentials. Stop the demo stack
before rebuilding and restart it afterward. The development and production
schemas are not the demo reset target.

## A short demonstration

1. Choose **Send a replacement**. Select customer `cust_north_3001`, case
   `case_north_2001`, SKU `sku-replacement`, and fee `500` minor units. Start
   the workflow. Show **Replacement completed**, then open each step's evidence.
   Refresh the page: the same workflow and recorded history remain.
2. Start another replacement for `case_north_2004`. The mock CRM permanently
   rejects it before creating an order. Show **Refund confirmed**, the failed
   order, and the refund link to the original charge. Explain that the refund is
   a separate action, with its own identity and evidence. The original charge
   stays in history; the replacement did not complete.
3. Start another replacement for `case_north_2002`. The CRM creates the order but
   loses its response and hides it from search. At demo pace the investigation
   window is 60 seconds; allow time for escalation. Show **Replacement needs
   investigation** and the missing refund step. The order may exist, so
   neither another order nor automatic compensation is justified.
4. Sign in as `demo-north-investigator`, locate that workflow's order in
   **Actions**, and inspect attempts, provider observations, contract limits,
   and the next check. The investigator can propose a bounded re-verification;
   a different human reviewer decides. Agents have no approval tool. Do not
   invent an applied-effect reference to make an uncertain case look complete.

On every workflow page, answer: **What happened? What evidence supports it?
What is the next safe step?** The timeline combines admission, transitions,
and evidence timestamps, with links to full step details. Step reads can refresh
at different times; the page discloses this and provides a refresh control.

If an admission response is lost, return to **Send a replacement** in the same
browser tab. The whole pending intent is retained per principal and tenant;
**Retry the same request** recovers the original identity. The intent is cleared
only after its matching workflow is shown. Session storage is not a replacement
for a business application's durable intent store; clearing storage or closing
the tab can remove it. Locate the existing workflow before starting another.

## Show the evidence and its limits

Use the [outcomes guide](outcomes.md) after the walkthrough. The saved F13 sample
has five workflows: one completed replacement, one confirmed compensation and
three unresolved outcomes. Useful completion is 2/2 declared achievable units;
it is not five completed replacements. The recovery packet also retains the
original F16 completion problems. Do not omit them from the presentation.

Worker crashes, lost refund responses, and other recovery interleavings are
covered by separate retained development evidence, not every interactive demo
path. The console demonstrates the product behavior; the independent ledger in
the development harness establishes observed external effects.

[View the implementation checks and screenshots](validation-20261022/REVIEW.md).

## Qualification and the three-minute demo

The full qualification run v1 is complete (2026-10-25):
[results](../phase-e/qual-v1/full/REVIEW.md). For the blueprint's three-minute
demonstration, with a seed command and exact clicks, see
[the demo walkthrough](../demo-walkthrough.md).
