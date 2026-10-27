# B3 execution development evidence, 2026-10-23

Uncommitted source on base `3269385`. This verifies an execution slice of the
independent conventional B3 baseline and a SafeAct lookup-completion fix.
B3 is not registered as a comparison arm. No frozen qualification, load test,
real Stripe call, paid model call or commit was performed.

## Implemented behavior

B3 now uses its own ordinary PostgreSQL transactions for admission, durable
publication, leased claims, token/version dispatch fences, actual observations
and reconciliation. HTTP and broker calls run outside database transactions.
Private worker, relay and reconciler processes construct their clients after
forking. Application roles cannot delete attempts, observations or audit records.
Retry decisions use the shared connector contracts, stable native keys, the
original retention window, attempt bounds, backoff and account pacing.

The [design](plans/b3-design.md) and [execution plan](plans/b3-execution-plan.md)
precede the implementation. Workflow/review runtime, normalized qualification
adapters, full registration and B0–B2 remain required. The code does not yet
provide a runnable comparison across all fault families.

Building this runtime reproduced a real SafeAct bug: a payment lookup could
start inside its key window, finish after actual expiry, and record authoritative
non-application despite an applied effect. The shared completion guard now checks
fresh database time after the answer and preserves uncertainty after expiry.
Positive matching receipts and actual pre-execution rejections retain their
existing meaning. The [binding plan](plans/lookup-completion-binding.md) explains
the counterexample. No frozen SQL, legal lifecycle pair, migration or grant
changed in this execution slice.

## Nine local development checks

The [raw observations](raw/) and [summary](raw/summary.json) retain nine cases:
seven confirmed completions and two expected unresolved outcomes, with nine
independent provider effects, ten dispatch calls, no duplicate effects and no
harness errors. Every owned process/schema was cleaned up. These are selected
development examples, not a frozen-corpus denominator or a performance result.

| Case | Observed result |
|---|---|
| Payments, CRM and email controls | Three confirmed completions, one effect each |
| CRM reply lost after application | Matching actual lookup confirms the single effect |
| Ambiguous email | One effect, no repeat, unresolved because there is no reliable lookup |
| Payments native-key replay | Two dispatches with the same key, one effect, confirmed completion |
| Worker killed after provider application | Actual SIGKILL exit `-9`, committed fence, no dispatch outcome; lookup confirms the effect |
| CRM late application after empty lookup | Empty HTTP answer precedes the actual effect; no unprotected repeat; later matching lookup confirms it |
| Lookup finishes after payment-key expiry | Actual six-second key expiry, no authoritative negative proof or extra dispatch, unresolved |

The [read-only replay](replay.py) independently checks final application-role
database snapshots against the provider ledger, actual positive references,
committed transaction identities before each provider dispatch, timing joins,
worker death and cleanup. It reproduces the [saved replay](raw/replay.json).
It is a small development verifier, not the main qualification oracle; no
cross-arm advantage, complete fault coverage or production safety is claimed.
HTTP traces for the killed worker are observed on the provider side and in
its private process trace; parent-side HTTP traces cover the other calls.

From the repository root, using the restored source overlay and running local
PostgreSQL services:

```sh
PYTHONPATH=. SAFEACT_RUN_BASELINE_DEV_CHECK=1 .venv/bin/python \
  docs/phase-e/dev-baseline-execution-20261023/smoke.py --out /tmp/b3-dev-new
.venv/bin/python docs/phase-e/dev-baseline-execution-20261023/replay.py \
  /tmp/b3-dev-new --out /tmp/b3-dev-new/replay.json
```

`--out` must be new. The [smoke script](smoke.py) uses loopback HTTP mocks,
fresh disposable schemas and private temporary process configuration. It
does not read Stripe credentials or register/launch a qualification arm.
Replay of this packet's `raw/` requires no services.

## Regression verification

| Final check | Tests passed |
|---|---:|
| Phase A discovery | 811 |
| Complete PostgreSQL/broker discovery, including isolated Redis | 587 |
| Isolated PostgreSQL security | 21 |
| API journeys | 9 |
| Scripted agent harness | 2 |
| Console unit | 37 |
| Browser journeys | 14 |

That is **1,430 distinct Python tests and 51 console tests**. The complete
database discovery passed in 919.758 seconds with no skips. Its total already
includes the nine B3 admission tests, thirty execution tests and the real
SafeAct lookup-completion regression. Focused reruns and development cases are
not counted again. Typecheck, lint, build and diff checks also passed.

The thirty execution tests include six concurrent duplicate deliveries,
concurrent fencing/lookup exclusion, an abandoned lookup reservation, actual
worker death, an in-flight request outliving its lease, replay after lost
commit acknowledgements, old negative proof scoped to earlier attempts,
retention expiry between claim and fence, actual HTTP lookup crossing expiry,
retry-after/backoff, breaker probe ownership, token-bucket bounds, publication
outside database locks and real Celery/Redis message loss followed by durable
republishing. The scheduler test alone uses synthetic retired rows to prove
they cannot starve newer due work; it makes no provider-effect claim.

[regression-runner.py](regression-runner.py) records the commands and original
workspace used for the security/API/agent/browser/database suites. Python tests
use `unittest discover`; all database, disposable-schema, security, broker and
isolated-Redis opt-ins were enabled for the final integration discovery.
[regression-checks.json](regression-checks.json) records original log hashes,
copied-log redaction counts and exact totals. Shared log copies redact free-form
64-hex strings; named digests in structured observations are preserved.

## Original failures retained

The [regression logs](regressions/) preserve these failures separately:

- The real SafeAct lookup test originally recorded `proven_not_applied` for
  an actual applied effect. The same test passes after the completion guard.
- The first B3 fence test expired its 100 ms lease before reaching the fence,
  while a hook incorrectly fired after a read-only refusal. Hooks now require
  an actual mutation and transaction identity; the lost-ack test verifies a
  committed attempt. Separate tests still exercise lease expiry.
- Initial full Phase A discovery exposed Celery's shared task registration
  polluting SafeAct's registry. Both factories now use private tasks; tests
  exercise both creation orders without weakening the existing registry check.
- The final sandbox-only Phase A attempt could not bind the test signer's
  loopback socket. The authorized rerun passed all 811 tests. This environment
  failure did not require a source, assertion or timeout change.

## Source and remaining work

The [source manifest](source-sha256.json) and [archive](source.tar.gz) retain
401 runtime/test files over the named base commit. Four unchanged base files
with scanner-shaped test literals are referenced by exact hashes instead of
copied. Restore them from that base. Credentials and private configuration are
excluded. Source/archive consistency, artifact hashes, local documentation
links, secret scans, unchanged HEAD and empty staging were checked.
[artifact-sha256.json](artifact-sha256.json) seals the packet. Earlier customer
support and F14/F15 packets remain unchanged; [historical-packets.json](historical-packets.json)
records their verified artifact manifests.

Next: implement B3 workflow and authenticated version-bound review, finish
baseline evidence adapters and B0–B2, audit achievable declarations and held-out
construction, then freeze corpus/scoring for STOP 2. Preserve the earlier F16
storm/outage shortfalls. The author must explicitly authorize the final run
after a prominent **FINAL BIG QUALIFICATION RUN** announcement. That run has
not started.
