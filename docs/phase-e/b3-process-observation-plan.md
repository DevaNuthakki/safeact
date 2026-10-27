# B3 process observations, 2026-10-23

Implement the observation boundary reviewed in the process adapter checkpoint.
The previous complete regression run (1493 Python, 51 console) is the baseline.
Read blueprint §§08–09 and the existing process/runtime/coverage code first.

Add B3-owned private append-only HTTP, execution and broker trace writers and
strict readers. Require a B3 disposable schema, owned private directory/file,
bounded records, append/read locks, complete JSON lines, exact event fields,
valid ids, real monotonic ordering and duplicate refusal. Never record headers,
payloads, credentials, arbitrary error messages or task bodies. Reject unsafe
files before writing any record bytes; use nonblocking opens and descriptor
checks so a FIFO entry cannot hang the harness. Emit fixed failure markers
without altering an observed action's business result; harness integrity must
recognize those markers. Retain the existing one-shot fault records and hooks.

Wrap Config's post-fork HTTP clients whenever its private fault/trace directory
is configured. Emit actual before-commit, commit-return and hook-error events
with B3's existing transaction ids, actual exception class and native operation/
workflow identity. Do not infer commit visibility from those events alone.
Observe actual Celery publisher/receiver/start/return signals, restricted to
the exact app/task/queue. Preserve B3's actual task result vocabulary. Publish
explicit UUID task identities and expose strict B3 envelope decoding. Avoid
global signal-handler leaks and cross-app observations.

Verify real PostgreSQL audit xid joins, HTTP/provider joins, lost acknowledgement,
separate worker SIGKILL/recovery, and real Redis/prefork publication/receipt/
execution. Missing/mutated observations must lose coverage. Keep SafeAct's trace
schema guard and protocols unchanged; permit B3's actual lost-ack exception in
the common coverage check with all existing xid/process/order joins required.
Run required complete regressions and retain source, raw observations, failures
and replay/counterexamples in a new packet. Earlier sealed packets stay intact.

This completes process observation plumbing, not the qualification arm, mock
native-key registration coordination, all fault drivers or F14 normalization.
No partial arm registration, held-out outcomes, corpus freeze, commit, real
Stripe/model call or final qualification is part of this slice. The prominent
final-run announcement and explicit user go after STOP 2 remain required.
