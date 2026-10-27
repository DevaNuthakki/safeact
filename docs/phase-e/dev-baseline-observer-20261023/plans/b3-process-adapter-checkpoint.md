# B3 process adapter checkpoint, 2026-10-23

Read-only follow-up review during database adapter regressions. This records
remaining work; none of the process changes below is implemented or qualified.

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
