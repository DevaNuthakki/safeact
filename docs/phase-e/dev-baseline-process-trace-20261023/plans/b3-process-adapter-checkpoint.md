# B3 process adapter checkpoint, 2026-10-23

Read-only follow-up review written during database adapter regressions. The
subsequent [process observation slice](b3-process-observation-plan.md) implements
the HTTP/execution/broker trace plumbing described below. Its
[development evidence](dev-baseline-process-trace-20261023/REVIEW.md) records
actual process observations. Stack ownership, native-key registration
coordination, fault drivers, F14 normalization and arm registration remain.

## Observation boundary

The new `chaos.baselines.observer.Observer.sample` returns root operation views
and database events for explicitly requested operations/workflows. Child jobs
appear in database events, not as extra admitted roots. Missing roots/children
or dispatch attempts/leases fail observation. Workflow snapshots retain
`binding_format=b3-request-v1` and the real immutable request; no SafeAct plan
is synthesized. Process adapters must propagate observation exceptions into
harness errors, including failures inside the provider's before-refund hook.

`chaos.baselines.processes.Config.runtime` currently constructs plain HTTP
clients. Its `Hook` writes B3-specific `hook`/`fault_fired` records containing
real commit ids, operation/workflow/attempt identity and process ids. These are
not the shared execution/HTTP protocols. SafeAct's `HttpTrace` constructor
accepts only `safeact_it_<uuid>`; passing a B3 schema to it fails. Do not change
that guard casually or rename B3's schema to gain access. Introduce explicitly
owned B3 writers with the same strict event validation, private regular files,
bounded records, append locking, no headers/payloads/credentials, error markers
and duplicate/partial-record detection. Reuse wire observation mechanics only
where their schema and ownership boundary is correct.

Wrap every process HTTP client, including reconciler clients created after
fork, with the existing transport observer. Preserve actual HTTP start/end,
method/path/correlation/payload digests and request ids. Join them to independent
provider-start/end records; do not infer a send from a runtime hook.

Commit observations need before/after-return/actual-exception events with the
same xid. Handle workflow-level ids explicitly. Preserve the real crash, park
and lost-ack hooks. Compare committed audit xids through independent database
samples; a selected failpoint is not proof of acknowledgement loss. Emit
actual Celery receive/start/finish/worker/process events and publication task
ids bound to committed outbox/delivery identities. A queue label does not prove
worker loss, broker death or redelivery.

## Stack, admission and faults

Own a B3 schema, private slot broker/queue, worker, relay, reconciler, advancer
and review API process. Use the same slot mock URLs through Toxiproxy. Stop
owned execution before closing provider admission, drain/release in-flight
calls and held commits, then take the final database/provider sample. Retain
cleanup errors. Respect pinned Uvicorn's drained SIGTERM exit `-15` for owned
API shutdown. Do not confuse an expected shutdown with an injected crash.

Normal B3 operation ids are assigned inside admission, while native provider
profiles must be registered before a worker can dispatch. The arm must close
this race explicitly, including workflow children admitted by the advancer.
Do not register keys after publication and hope the worker is slower. Keep
registration in harness/provider infrastructure; application workers must not
read the independent ledger or receive privileged mock admin capabilities.

Map each fault to the actual B3 transaction stage and owned process, preserving
the required HTTP/lease/commit/broker interleaving. Current hook names differ
from SafeAct's; fault-file names are not interchangeable. Review F05/F06/F08/F11
joins before registration. Workflow faults must target the actual separate
inverse and sample charge/order/inverse bindings before its effect.

F14 needs authenticated displayed bindings, overlapping reviewer HTTP requests,
real PostgreSQL wait graphs, committed winner/refusal/replay observations and
one-use authority. B3's own native proposal/decision records require explicit
normalization, not fabricated SafeAct audit labels. F15 alone is N/A for B3.

## Registration gate

Do not register a partial `b3` arm in `chaos.qualify`, silently skip unsupported
families, or count an internal stage as coverage. Validate all applicable
development families through the common runner with complete raw observations
and cleanup. Preserve unresolved outcomes and robust-baseline successes.
Then complete B0–B2 and construction/scoring/achievability audits before corpus
freeze and STOP 2. No held-out outcome tuning, final qualification, real Stripe
or model call, commit, staging or push is authorized by this checkpoint.

## Reviewed registration handshake candidate

The process-observation review identified a concrete next implementation point:
`Runtime.relay_once` commits its delivery reservation, calls
`after_publication_commit`, and only then enters broker publication. A harness
registration handshake at that hook can cover ordinary jobs and advancer-created
children without exposing mock administration to worker/advancer processes or
holding their PostgreSQL transaction open while waiting. This is a candidate
for the owned-stack slice, not an implemented mechanism or performance claim.

Every owned relay would announce a unique batch/generation after the real
commit and wait for a matching harness acknowledgement before sending any task.
The harness would independently read committed immutable job bindings, register
each native provider key with its declared mock contract, and acknowledge only
after registration succeeds. Reading all not-yet-registered jobs in the owned
schema is sufficient to include the committed batch; it avoids depending on
delivery timestamps or converting PostgreSQL xid32 row metadata into xid8.
Remember each key's original contract and fail a changed binding. An already
registered identical binding is harmless. A rollback without publication may
leave a profile declaration, never a provider effect or stored native response.

Use private, bounded records and generation ids that cannot match a stale
acknowledgement. Keep this handshake distinct from one-shot fault files and
their parked-process marker. Timeout, malformed data, controller death or
registration failure must fail the harness; do not publish by default. The
same gate must apply to every relay instance and each replayed reservation,
including lost publication acknowledgements. Controller startup must precede
owned execution, and teardown must stop/reap relays and the controller without
leaving a waiter or an admitted intent unaccounted for.

Before adoption, test real first-publication races, concurrent workflow
advancement, rollback/replay, lost acknowledgements, stale acknowledgements,
controller failure and shutdown. Record the gate's overhead and review how it
is applied/calibrated across arms before any latency comparison. It must not
alter provider contracts, investigation/attempt budgets or fault observations.
