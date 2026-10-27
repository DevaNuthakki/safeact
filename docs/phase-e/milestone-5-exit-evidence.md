# Milestone 5 exit evidence: qualification, load, packaging and demo

Milestone 5 delivers the blueprint's Phase E: the remaining Phase D exit work (Stripe
sandbox, F16), the B0–B3 comparison over 1,200 frozen scenarios, the load test,
Compose packaging with a fresh-clone check, and the README, demo walkthrough and
evidence pack.

The claim stays within the declared tested contracts: zero observed prohibited
duplicate effects. It is not universal exactly-once and not production approval. The
production boundary stays deny-only.

The checklist against every blueprint requirement is the
[evidence pack](../evidence-pack.md). This document records what Milestone 5 did, its
results, its tests and what remains.

## Sub-steps

| Step | What | Evidence |
|---|---|---|
| M5.1 | Current-state projection (0017), written only in the protected path; drift checked before every disposable schema is dropped | [projection.md](projection.md) |
| M5.2 | HTTP mock providers and separate-process entrypoints (process stack) | `tests/integration/test_stack_processes.py` |
| M5.3 | Stripe test-mode adapter, offline (raw HTTPS, pinned version, `rk_test_` gates) | [stripe-sandbox-contract.md](../phase-d/stripe-sandbox-contract.md) |
| M5.4 | F16: backoff, limiter, breaker, paced lookups (0018) | [f16-limits.md](../phase-d/f16-limits.md), `tests/integration/test_provider_limits.py` |
| M5.5 | Real separate Celery worker killed (SIGKILL) | `tests/integration/test_worker_process_kill.py` |
| M5.6 | Next verification check in the API, MCP and console (0019) | `tests/integration/test_next_check.py`, `ui/e2e/next-check.spec.ts` |
| M5.7 | Structured logs and Prometheus metrics, allow-listed labels (0020) | [observability.md](observability.md) |
| M5.8 | Stripe sandbox run, 16/16 PASS, 77/150 calls; Phase D exit | [REVIEW-M58](../phase-d/stripe-sandbox-v1/REVIEW-M58.md), [Phase D exit](../phase-d/phase-d-exit-evidence.md) |
| M5.9 | Qualification harness (Toxiproxy, failpoints, independent ledger, oracle) | [recovery](dev-recovery-20261022/REVIEW.md), [workflows](dev-workflows-20261022/REVIEW.md), [boundaries](dev-boundaries-20261022/REVIEW.md) |
| M5.10 | Baselines B0–B3 | [B3 design](b3-design.md), [execution](dev-baseline-execution-20261023/REVIEW.md), [workflow/review](dev-baseline-workflow-review-20261023/REVIEW.md), [adapters](dev-baseline-adapter-20261023/REVIEW.md) |
| M5.11 | Frozen corpus (1,200 scenarios, 80-case smoke sample), scoring and qualification command; STOP 2 | [STOP 2 packet](stop2-post-pooling-20261024/REVIEW.md), `chaos/manifest_qual_v1.json` |
| M5.12 | Slot calibration (4 slots), the primary full run v1, and the load-suspect diagnostic | [calibration](qual-v1/calibration/REVIEW.md), [full run](qual-v1/full/REVIEW.md), [diagnostic](qual-v1/diagnostics/REVIEW.md) |
| M5.13 | Load test | [load-v1](load-v1/REVIEW.md) |
| M5.14 | Compose full stack and fresh-clone check (A12) | [compose-v1](compose-v1/REVIEW.md) |
| M5.15 | README, demo walkthrough and seed, optional Stripe test-mode demo profile, evidence pack, this document | [README](../../README.md), [demo-walkthrough.md](../demo-walkthrough.md), [evidence-pack.md](../evidence-pack.md) |

## Results

**Qualification v1 (primary, 2026-10-25):** 6,000/6,000 rows, 0 harness errors.

| Arm | Safety-failure rows | Prohibited duplicates | Orphans | Lost intents | Useful / achievable |
|---|---:|---:|---:|---:|---:|
| SafeAct | 0 | 0 | 0 | 0 | 924/937 (98.6%) |
| B3 | 0 | 0 | 0 | 0 | 841/874 (96.2%) |
| B2 | 92 | 92 | 0 | 0 | 558/691 (80.8%) |
| B1 | 239 | 213 | 0 | 0 | 426/691 (61.6%) |
| B0 | 250 | 371 | 0 | 0 | 149/438 (34.0%) |

**Load test (M5.13):** 14,710 operations; 0 duplicates; 0 drift; 0 deadlocks, lock
timeouts or serialization failures.
- **Saturation:** execution saturates at about 40–48 operations/s with 4 workers and
  20 ms mocks, and one API process admits up to 120/s.
- **Limiter:** it held 25 dispatches/s.
- **Relay:** it republishes backlog (2.25 deliveries per operation). The claim refuses
  the copies; this is recorded as an observation.

**Fresh-clone check (M5.14):** PASS warm (33 s) and cold (`--no-cache --pull`, 103 s).

**Stripe sandbox (M5.8):** 16/16 PASS.

**Stripe test-mode demo profile (M5.15, one real run):**
- **Normal refund:** `pi_3UUbA1DnpP0MDY8U4RYP3jpm`, SUCCEEDED, refund POST
  `req_QbeYYZ6EmdU39C`, 1 refund at Stripe.
- **Dropped-response refund:** `pi_3UUbA2DnpP0MDY8U0R6eGrx5`, UNKNOWN → VERIFYING →
  SUCCEEDED by lookup, one refund POST `req_BiLvrB1WPbI0PE`, exactly 1 refund at
  Stripe.
- **Record:** [stripe-demo-v1](stripe-demo-v1/REVIEW.md).
- **What SUCCEEDED means here:** Stripe accepted the request and created the refund.
  Both refunds reported the status `succeeded` at response time. That is not a live
  value, and settlement to the customer is out of scope.

**Agents (M4, held-out):**
- **Guarded:** 66/72 correct under the strict rule (v1), 71/72 under the rule frozen before
  the held-out run (v2), with 0 unsafe effects.
- **Direct tools:** 39/72 under both rules, with 28 unsafe effects.
- **Deterministic caller:** 72/72 under both rules. That shows the value of the constrained
  tools, not better model reasoning.
- **What the rule change touches:** all 5 trials that change verdict are one scenario,
  AG1-H07. The rules and their timing are in the
  [README](../../README.md#agents-milestone-4-held-out).

## M5.13–M5.15: what changed after the primary run

The primary run is spent. Every later change to the pinned source inventory is
declared in
[post-qualification-changes.json](qual-v1/post-qualification-changes.json), with its
sub-step and reason.

- **What the freeze test checks:** `tests/phase_a/test_qualification_freeze.py` checks
  that the list is exact, that the manifest digest is the pinned one, that no lifecycle
  SQL, migration, `core/lifecycle.py` or provider-key file changed, and that a frozen
  run from this tree is refused.
- **Admission path:** two declared changes touch it (`api/catalog.py`,
  `api/actions.py`, for the console-only Stripe demo action). For every existing action
  the admitted values are unchanged.

**Defects found and fixed in these steps** (also in [observations.md](observations.md)):
- **Compose smoke test re-ran the bootstrap:** a plain `compose run smoke` re-ran the
  one-shot bootstrap under live services. The smoke test now runs with `--no-deps`.
- **Cached HTML served to the console's fetch:** the browser reused cached
  `index.html` for the console's fetch after a direct navigation. nginx now sends
  `Vary: Accept`, and Phase A checks it.
- **WIP Stripe draft would have been refused:** the interrupted session's draft used an
  action type that the Stripe adapter does not accept. The rework admits
  `refund_payment` on `stripe-test` and leaves the adapter unchanged.

**Observed, not changed:** the relay republishes under backlog. A fix would change the
qualified system, so it is not evaluated.

**Clock:** no clock jump was observed in any suite run (wall clock against monotonic
time per suite), nor in the load test (host-to-PostgreSQL offset sampled every second;
largest spread in any step 5.9 ms).

## Tests on the final M5.15 tree (local, 2026-10-25)

| Suite | Result |
|---|---|
| Phase A | 962 tests, OK |
| PostgreSQL + broker | 715 tests, OK (Stripe offline runner 16/16 PASS inside it) |
| Isolated security cluster | 21 tests, OK |
| API end to end (incl. the Stripe demo profile and walkthrough seed) | 14 tests, OK |
| Agent harness | 2 tests, OK |
| Console: Vitest | 38 tests, OK |
| Console: Playwright | 14 tests, OK |
| Console: typecheck, lint, build, `check:api`; `export_openapi --check` | clean |

Python total: 1,714. Wall clock against monotonic time was recorded around every suite,
and no clock jump was observed.

**GitHub CI passed** (private development history: PR #7, run 40252297269, commit `26e9761`), with all
four jobs green (Phase A, PostgreSQL + broker integration, isolated security, console).
- **Earlier runs:** the first three runs failed on test defects that only the slower
  runner exposed: a Phase A dependence on a local `.env`, B3 retry-backoff and
  worker-readiness timing, and a single log read. All were fixed in the tests; no pinned
  source changed ([observations](observations.md)).
- **`fresh-clone.yml`** triggers on a push to main. Added 2026-10-27: it has since passed on
  GitHub on every merge to main (private development history).

## Not done, and why

- **The demo video:** not produced (author decision, 2026-10-27); README screenshots and
  the guided walkthrough instead.
- **`fresh-clone.yml` on GitHub:** done since this report; it has passed on every merge to
  main (added 2026-10-27, private development history).
- **The optional full qualification run in CI** (neutral hardware): planned, not run.
- **Publishing the compressed raw full-run output as a Release asset:** done since this
  report (2026-10-27): assets of the
  [v1.0.0 release](https://github.com/DevaNuthakki/safeact/releases/tag/v1.0.0), mapped
  to their sealed digests in [release-assets.md](qual-v1/release-assets.md).
- **Open limits carried forward:** worker-side identity binding; runtime error-log
  attribution; SQL canonical-JSON enforcement; runtime credential/subject binding;
  agents get one operation per target; asynchronous Stripe refund failure is out of
  scope.
