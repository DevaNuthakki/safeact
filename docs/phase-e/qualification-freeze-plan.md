# Frozen comparison inputs and execution gate

Follow the author's accepted M5.11/M5.12 plan: 1,200 scenarios, five arms,
60 controls (5%), 120 held-out combinations (10%), and 1,020 F01–F16 cases.
Use 64 cases for F01–F12 and 63 for F13–F16. Each of the six combinations
has 20 cases. The qualification seed is disjoint from development. A frozen
80-case smoke sample covers all families, workflow modes and broker boundaries.

STOP 2 reviews inputs before any qualification scenario is executed, including
smoke. Slot calibration happens after that review, at 1, 2, 4, 6 and 7 slots
(the supported reserved Redis DB count). Choose by sampled load only, never by
safety/usefulness results. Record every candidate and both original and diagnostic
rerun outcomes. Full execution requires the resulting slot amendment; it cannot
substitute an arbitrary count. The no-commit instruction overrides the older
plan's amendment-commit wording: use reviewed file and source digests instead.

Implement a separate frozen executor reached by `chaos.qualify --manifest`.
It requires an explicit qualification environment gate, verifies the complete
input manifest and source inventory before service access, refuses dev filters,
and never uses Stripe or a model. A pinned immutable-input digest excludes only
the separate post-review calibration amendment. No full outcome may predate that
amendment. Missing calibration blocks full execution. Do not execute it now.

Budget: one primary full comparison, at most 6,000 scenario/arm rows, maximum
24 hours for the invocation, up to seven reserved local slots. An active case
finishes its owned cleanup when the scheduling budget expires; no new case starts.
Missing cases are an incomplete run, never successes. Any SafeAct safety failure
stops new scheduling and blocks qualification even if a diagnostic rerun passes.

Load-suspect classification is frozen: a failed row in F05/F06/F07/F08/F11/F16,
or one with sampled load above the core count, or observation overrun >1 second.
Diagnostic reruns use the same inputs and stay separate from headline originals.
No automatic diagnostic reruns consume budget in the primary invocation.

Source pinning includes the harness, connectors, core, storage, API, MCP and
deployment packages, SQL and dependency locks. The small digest anchor and
manifest are excluded from their own source inventory to avoid recursive hashes;
tests verify the anchor against the manifest. Evidence and live status documents
are separate artifacts. A source or input change invalidates the candidate.

The manual qualification workflow may be prepared, but no workflow dispatch,
smoke calibration, full comparison, commit or paid/external execution occurs
before STOP 2 approval. CI execution remains unverified until separately run.
