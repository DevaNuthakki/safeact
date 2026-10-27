# B0–B2 execution and attribution plan, 2026-10-23

Read blueprint §09 and the B3 design. This completes the already authorized
pre-STOP-2 work. No commit, final qualification or held-out execution is authorized.

B1/B2 reuse ordinary intent/outbox/attempt record plumbing and the fixed
workflow, not B3 recovery choices. A queue delivery records its start in a short
transaction but may start again while an older delivery is still running.
Outcomes use the current job row without ownership fencing. A visibility/retry
deadline can requeue abandoned/running work; it does not fence the provider.
Preserve actual immutable attempt deadlines and all late results. Retry within
the shared attempt/deadline/pacing budget, with the same bounded backoff and
Retry-After; never call a connector lookup or use SafeAct protected SQL.
Keep ambiguous exhausted work pending/unknown, rather than claim definitive
failure or a human escalation these arms do not implement.

B1 uses a fresh payment key per actual attempt. B2 reuses its operation's native
payment key; CRM/email still have no native idempotency. Harness registration
predeclares the bounded derived keys before any publication; workers get no
mock administration. Stable or fresh wire identities remain visible in raw
data. Add an explicit validated execution mode to private baseline config;
legacy B3 config and semantics remain the default. Keep queue fault controls
strictly scoped to owned slot databases, queues, messages and processes.

For early redelivery, the shared driver must select and restore the same real
unacked message. A weaker baseline may execute the duplicate rather than refuse
it. Preserve that result; do not require B3's safe behavior as fault validity.
For this explicit queue mode, independently observe the original committed
attempt and its still-pending outcome, native current job state and original
deadline. Join those facts with the original live provider/HTTP interval. Keep
the existing robust B3/SafeAct dispatch proof and refusal checks unchanged.

B0 runs bounded synchronous calls in separately killable subprocesses with
JSON-only stdin/stdout control frames and private logs/config/traces. It has no
database intent or broker. The child announces its actual accepted operation
identity before any provider call and waits for the caller's receipt; the
harness retains that observed acceptance in memory. A killed child leaves
the accepted request pending/unknown; the harness must not invent a durable
B0 intent or forget effects after lost responses. Native mock-key registration
is a bounded matching control exchange before HTTP, never child mock-admin
access. Every submitted copy is a new unguarded call, and every retry has a
new key. Process death selects a real owned child; it never kills the observer.

All arms execute the same authorized charge/order/separate-refund workflow.
For B0, retain actual child process snapshots explicitly as process observations,
not fabricated database rows. An acknowledged process snapshot before the
inverse records its actual charge reference, amount and confirmed order failure.
The common oracle validates that native representation against the same
independent effects and payloads. For B1/B0, retain every actual derived wire
identity across retries so duplicates remain attributed to their authorized
step rather than disappearing into an orphan bucket. Validate all aliases
against the native key strategy and actual attempt ordinals.

Applicability is declared from capabilities before weaker-arm outcomes:
human-review and agent-only cases require those surfaces; broker faults require
a broker; commit-ack faults require durable transactions; lease-expiry cases
require a persisted retry/ownership deadline; lookup-triggered late commits and
lookup-violation cases require lookup; post-escalation triggers require actual
human escalation. Record precise per-case reasons for genuine feature N/A.
Do not relabel a missing implementation N/A. Provider timeouts, effects,
duplicates, changed submissions, bounded retries, native retention, outages
and fixed workflows remain evaluated wherever their mechanisms apply.

Test lost acceptance/accounting and actual child death, fresh versus stable
keys, duplicate in-flight delivery, late outcomes, per-connector differences,
budget/deadline exhaustion and separate inverse attribution. Run the common
development corpus with retained raw results and cleanup, preserve legitimate
baseline successes/failures, then audit applicability/scoring/construction and
complete regressions before STOP 2. No held-out outcome tuning or artificial
failures to make SafeAct win.
