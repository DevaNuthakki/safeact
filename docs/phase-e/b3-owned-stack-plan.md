# B3 owned stack and registration handshake, 2026-10-23

Read blueprint §§05, 08–09 and the process adapter checkpoint before this
slice. The preceding complete regressions (1513 Python, 51 console) are the
baseline. No final qualification, held-out outcome, commit or real provider/
model call is authorized here.

Implement the reviewed post-commit/pre-publication registration handshake.
An explicitly configured relay writes a bounded private request containing its
schema, real pid, fresh UUID generation and actual publication-commit xid,
then waits outside its PostgreSQL transaction. The harness controller reads
committed immutable job bindings on a separate read-only connection, registers
native mock keys/contracts and acknowledges that exact request only on success.
Remember original bindings and reject changes. No worker/advancer receives mock
administration or independent provider-ledger access. Other process behavior,
contracts, deadlines, attempt budgets and recovery choices remain unchanged.

Use private owned regular single-link files, locking, atomic complete-message
publication, exact fields/identity and bounded deadlines. Stale/foreign or
malformed confirmations, controller failure and timeouts never permit task
publication. Every relay instance and replayed reservation uses the gate.
Shutdown can cancel a wait without publishing or abandoning the durable job.
Record request, database query/registration and completed-wait observations;
report this harness overhead separately before any performance comparison.
Keep one-shot fault semantics intact, including lost publication acknowledgements.

Add a harness-owned B3 stack using its own disposable Environment, private
configuration/queue, independent registration controller, and separately owned
worker, relay, reconciler, advancer and optional review API process groups. Wire
slot provider proxy URLs and broker without flushing or owning the slot itself.
Keep every process generation for cleanup. Reject duplicate live spawns and
retain actual kills/restarts and unexpected exits. Reuse checked process-group
quiescence, stop execution before provider admission closes, drain provider
calls/held commits before final snapshots, and only then drop owned DB resources.
Retain cleanup failures, and remove private configuration on final close.

Test real first-publication blocking, row-lock-free waiting, exact registration
before actual broker publication, new workflow charge/refund children, replay/
lost acknowledgement, concurrent advancement, failed controller/registration,
stale confirmations and graceful shutdown of waiting relays. Exercise owned
API health/login, worker kill/restart generations and complete quiescence.
Run required regressions and seal a new source/evidence packet; preserve all
eight previous packets. No partial B3 arm is registered; common fault drivers,
F14 normalization, B0–B2 and construction/scoring audits still follow.
