# Work authorized through STOP 2, 2026-10-23

The author authorized continuing the remaining implementation and development
validation, with the next approval checkpoint at **STOP 2 — PRE-RUN REVIEW**.
Keep all work uncommitted. Do not start frozen qualification, held-out outcome
runs, production execution, paid models or real Stripe calls. The latest
complete baseline is 1537 Python / 51 console tests, typecheck/lint/build.
Existing sealed evidence stays immutable. Read blueprint §§05, 08–09 and the
B3 design/process checkpoint before this plan.

1. Complete B3's explicit common-runner adapter. Reuse owned processes and
   read-only observers, not SafeAct protected SQL. Add strict baseline broker
   decoding/queue validation selected explicitly by the adapter; SafeAct remains
   the default protocol. Support actual queued loss, isolated Redis death and
   in-flight restoration with independent committed B3 delivery/fence queries.
   Route failpoint arming/fired observations through an optional arm interface;
   B3 uses its own private files and validates parked child ownership before
   killing a process group. Keep native transaction stages and error classes.
   Prove all applicable development interleavings before registry publication.
   F06 observation review found that clearing the mutable job lease loses its
   original fence identity. Persist the actual fence version/expiry in the
   append-only B3 attempt at the existing fence transaction; independently
   compare that stored deadline with DB time even after reconciliation changes
   the job. This records existing behavior and changes no lease duration,
   state decision or retry budget. Refuse inconsistent active bindings.
2. Complete B3 workflow/F14 drivers. Add an authenticated read endpoint for the
   actual persisted proposal so both reviewers see its exact native binding.
   Independently observe native proposals, decisions, evidence, audit and humans
   without hashes/sessions/passwords. Explicitly map native meanings in common
   boundary coverage, retaining their native kinds and committed versions;
   never invent SafeAct audit records. Prove two HTTP requests blocked on the
   real job lock, winner/refusal/replay, stale evidence and one-use retry.
   Preserve separate inverse effects and before-refund observations.
3. Implement B0/B1/B2 with documented capability boundaries. B0 synchronous calls
   run in killable owned processes; harness attribution must preserve uncertain
   admissions without giving B0 a synthetic durable intent. B1 has durable jobs
   and bounded late-ack retries; B2 adds stable keys only where the provider
   guarantees them. Both receive the fixed workflow semantics and shared
   contracts. Honest weaknesses/results and feature-based N/A cells remain.
4. Run common development families for all applicable arms. Audit scenario
   control/provider compatibility, F13 combinations and inverse targeting,
   timing/achievability, outcome accounting and scoring from contracts/source.
   No held-out executions or outcome-based tuning. Preserve original failures
   and reruns. Run complete required regressions and retain source/evidence.
5. Freeze the reviewed corpus, contract/timing/coverage/scoring/source/budget
   inputs, produce the concrete pre-run packet and stop. Announce
   **STOP 2 — PRE-RUN REVIEW** and wait for the author's explicit authorization
   before the final big qualification run. Compose/demo follow separately.

Development subsets may instantiate an unregistered adapter directly. Do not
publish a partial B3 arm or silently mark unimplemented families N/A. No
coverage comes from a configured fault alone: retain real HTTP, provider,
transaction, broker, process, lock and cleanup joins. Check full regressions
after each coherent implementation stage and update CLAUDE.md current status.
