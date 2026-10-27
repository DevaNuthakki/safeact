# M5.9 workflow and compensation qualification

Implementation plan, 2026-10-22. Blueprint §§05, 08 (F13), 09 and A6 require
separate identities and counts for each forward step and its inverse. The
existing fixed workflow is charge → replacement order → conditional refund;
the former F13 template incorrectly described charge → email and did not
actually create a workflow.

The harness will submit one authorized workflow through the existing durable
workflow admission path. The real relay, worker, reconciler and advancer
processes execute its children through HTTP/Toxiproxy. This work changes the
development harness, not the protected lifecycle or frozen migrations.

Independent committed database snapshots retain the workflow plan, step ids,
payloads, native-key digests, compensation linkage and states. Provider-side
call payload digests and effect ids establish what actually happened. The
oracle will validate both sources against the scenario's authorized plan:
a refund must target the independently observed original charge, for exactly
its amount, and must retain a separate operation and correlation identity.
Unknown effects remain orphans. Forward duplicates and compensation duplicates
are reported separately; either blocks SafeAct's safety gate.

Development variants cover a lost refund response verified by lookup, a refund
with persistently lost responses and hidden lookup, a permanently rejected
refund, an uncertain order that must never trigger compensation, and a normal
completed workflow. Faults target refunds specifically so the initial charge
cannot consume a compensation fault. Actual HTTP/provider observations, not
the configured mode, establish fault coverage.

Useful completion uses the pre-cleanup observation window. Final provider
effects and final DB bindings still count for safety; cleanup cannot create
a verified-completion result. A workflow claiming restored state without a
valid inverse effect is a false claim. Counts and raw steps preserve the
forward history when compensation succeeds.

Counterexamples will include missing and duplicate inverses, wrong target or
amount, shared identities, orphan effects, uncertain order, forged restoration
claims, missing committed snapshots and cleanup-only compensation. A real
development run must prove each enabled path before F13 is enabled by default.
No commit, Stripe/model call or frozen-corpus execution is part of this work.

## Implemented SafeAct development slice

`chaos.scenarios.WorkflowSpec` now describes the authorized fixed workflow.
`chaos.arms.workflows` admits it and observes its real children;
`chaos.workflow_trace` reads committed bindings on a separate connection.
`chaos.workflow_faults.WorkflowPayments` targets refund replies without
consuming the initial charge's fault. Observer failures fail the harness even
when the mock HTTP API translates the original exception to a provider error.

`chaos.workflow_oracle` compares the committed plan, step identities, payload
digests and provider effects. Its inverse check requires an independently
observed successful charge and confirmed order failure before the inverse
effect, not merely final states that changed afterward. `chaos.workflow_coverage`
joins provider intervals to uniquely attributable completed HTTP exchanges;
missing or ambiguous observations earn no coverage.

F13 has five explicit development variants. A workflow is one useful-completion
unit, with separate per-step effect/call/evidence scores. Forward duplicates,
duplicate inverses, invalid inverses and false restoration claims all block
the SafeAct gate. The initial charge/order history remains visible after a
verified refund. [Final development evidence](dev-workflows-20261022/REVIEW.md)
retains raw observations, source hashes and the scoring replay.

This observer adapter currently targets SafeAct. Baseline adapters remain
M5.10 work and must use independent evidence from their own implementations.
In particular, requiring the nonpersistent B0 arm to acquire SafeAct's SQL
workflow tables would change that baseline unfairly. Its evidence adapter
must retain the harness's observed requests/effects without adding durable
coordination to the system under test. Cross-arm attribution is not frozen.
