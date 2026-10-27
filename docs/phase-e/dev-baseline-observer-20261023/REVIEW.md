# B3 database adapter development evidence, 2026-10-23

**FINAL BIG QUALIFICATION RUN: NOT STARTED. No commits.**

B3's independent read-only adapter now emits committed operation, audit,
dispatch-lease, investigation-deadline and workflow samples. Every sample uses
a fresh repeatable-read/read-only transaction, with host monotonic query
intervals and database-clock expiry predicates. Sampling neither locks jobs
nor calls B3 runtime/workflow methods or SafeAct SQL. It never reads provider
truth, credentials, password hashes or sessions. Missing admitted identities,
original charge bindings or dispatched-attempt/lease bindings fail observation.
The application role and product authorization are unchanged.

The common workflow oracle accepts B3's immutable request representation and
actual child bindings. It checks tenant/requester/step identity, customer,
case/SKU/amount, all three contracts, per-provider accounts, distinct child
identities, and the inverse's exact original charge reference. B3 legitimately
uses a distinct CRM account and an actual-charge refund resource. No SafeAct
plan or rewritten resource is manufactured. Provider calls/effects and a
committed pre-inverse snapshot remain necessary to establish restoration.
Unknown formats and malformed persisted plans fail scoring.

The [implementation plan](../b3-observer-plan.md) precedes these changes.
The [raw development records](raw/summary.json) retain five actual disposable
PostgreSQL/HTTP cases: normal completion, verified compensation, lost inverse
reply, hidden inverse effect and rejected inverse. They contain six forward
effects and three inverse effects, with no oracle safety failures and complete
cleanup. Hidden/rejected inverses remain unresolved. These are selected adapter
checks, not the qualification runner, fault-family coverage, a comparison, a
latency benchmark or a load result. Their scenario envelope uses a common
authorized business request; local test code supplies each case's actual fault.
Do not interpret its generated F13 id as qualification-variant coverage.
The [negative coverage check](coverage-boundary.json) confirms that all five
records receive no shared interleaving coverage without process observations.

Verification passed: **1,493 distinct Python tests** (825 Phase A, 636
PostgreSQL/broker, 21 isolated security, nine API and two scripted-agent harness)
and **51 console tests** (37 Vitest, 14 Playwright). Typecheck, lint, build and
diff checks passed. Full integration discovery passed all 636 in 959.814 seconds
without skips. This includes the twelve new observer integration tests and
eight new offline oracle tests. The five retained executions reuse integration
cases and are not counted again; the 27 synthetic mutations are also separate.

The [regression record](regression-checks.json) retains exact commands, flags,
timings, log fingerprints and diagnostics. The [replay result](replay.json)
matches all five new B3 records and all seventeen saved SafeAct workflow/boundary
results. [Counterexamples](counterexamples.json) reject wrong root/child
identities, accounts, contracts, resources, charge references and amounts,
missing/uncertain precursor evidence, missing effects and duplicate inverses.

The [source manifest](source-sha256.json) and [archive](source.tar.gz) capture
413 runtime/test files, with four unchanged scanner-shaped fixture files
referenced by exact base hashes instead of copied. The [source delta](source-delta.json)
adds three files and changes only `chaos/workflow_oracle.py`; 409 earlier source
files remain unchanged. [Historical verification](historical-packets.json)
checks 211 artifacts across six previous packets without rewriting them.
The [artifact manifest](artifact-sha256.json) fingerprints this packet. Archive,
current-source, secret and documentation-link checks passed; HEAD and staging
remain unchanged.

Replay from the repository root, without services:

```sh
PYTHONPATH=. .venv/bin/python docs/phase-e/dev-baseline-observer-20261023/replay.py docs/phase-e/dev-baseline-observer-20261023
PYTHONPATH=. .venv/bin/python docs/phase-e/dev-baseline-observer-20261023/counterexamples.py docs/phase-e/dev-baseline-observer-20261023
```

To reproduce the five local cases, explicitly enable
`SAFEACT_RUN_BASELINE_DEV_CHECK=1`, `SAFEACT_RUN_DB_INTEGRATION=1` and
`SAFEACT_RUN_DISPOSABLE_DB_TESTS=1`, then invoke [devcheck.py](devcheck.py) with
`PYTHONPATH=.` and `--out` pointing to a new directory. This requires the local
development PostgreSQL and mock sockets; it makes no external provider calls.

The first replay invocation omitted `PYTHONPATH=.` and failed import. With that
corrected, direct Python equality compared tuples with their serialized JSON
arrays. The checker now compares JSON-normalized results; this does not change
oracle scoring or expected values. Both original diagnostics are retained.

The [process adapter checkpoint](../b3-process-adapter-checkpoint.md) records
the reviewed next steps, including trace schema restrictions and the race
between native-key profile registration and worker dispatch. It is a plan,
not implemented process evidence.

Still required: B3 process/HTTP/broker observations and fault adapters, the F14
review driver, B0–B2, common construction/achievability/scoring audits, corpus
freeze, STOP 2 and explicit final-run authorization. No partial B3 arm is
registered and no unsupported fault is silently labeled N/A. Real Stripe and
model calls, held-out outcomes and final qualification were not run.
