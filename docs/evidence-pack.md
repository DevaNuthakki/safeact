# Evidence pack

Every requirement of the SafeAct Revision 2 blueprint's §11 (acceptance checks A1–A12),
§12 (demonstration, required evidence, result fields) and §13 (definition of done),
linked to the file and test that support it.

The blueprint's rule applies throughout: any failing or unmet check narrows the
published claim. Narrowings are stated in the row and collected at the end.

Status values:
- **Met:** evidence exists and its tests pass.
- **Met, narrowed:** met within a stated limit.
- **Pending:** not yet done.

Test suites and their opt-in flags are listed in `CLAUDE.md`. On the M5.15 tree they
passed locally; the counts are in
[milestone-5-exit-evidence.md](phase-e/milestone-5-exit-evidence.md).

## §11 acceptance checks

| Check | Status | Evidence | Tests |
|---|---|---|---|
| **A1** No network dispatch before durable intent | Met | [Phase B exit, A1](phase-b/phase-b-exit-evidence.md#a1--no-network-dispatch-before-durable-intent) | `tests/integration/test_durable_execution_happy_path.py` (the provider sees a committed intent and fence on every call; unadmitted, uncommitted or rolled-back admission never dispatches; a lost fence ack never calls) |
| **A2** Illegal/stale transitions and changed-payload key reuse rejected | Met | [Phase B exit, A2](phase-b/phase-b-exit-evidence.md#a2--illegalstale-transitions-and-changed-payload-key-reuse-rejected); state machine in `core/lifecycle.py`, frozen SQL in `storage/lifecycle_sql/` | `tests/integration/test_lifecycle_transitions.py`, `test_lifecycle_privileges.py`; Phase A `test_migration_sql_frozen.py` and the union-of-pairs test; SAT22 in `test_api_actions.py` |
| **A3** No unsupported automatic retry of ambiguous non-idempotent work | Met | [Phase C exit, acceptance checks](phase-c/phase-c-exit-evidence.md#acceptance-checks); qualification F01/F06 on email and CRM | `test_f01_email_escalates_at_once_and_is_never_resent`, `test_f06_email_lease_expiry_escalates_and_never_resends`, `test_sql_refuses_unprotected_retries_directly`, `test_f09_retention_lapse_before_the_fence_blocks_the_replay` |
| **A4** No forbidden duplicate effects in the declared qualification suite | Met, narrowed (declared contracts, mocks, one laptop) | [Full qualification run v1](phase-e/qual-v1/full/REVIEW.md): 1,200 scenarios × 5 arms; SafeAct 0 prohibited duplicates, 0 orphans, 0 lost intents | `chaos.qualify` with the frozen manifest (`tests/phase_a/test_qualification_freeze.py` checks the inputs) |
| **A5** Every accepted intent accounted for; unresolved distinct from completed | Met | Phase C exit (A5); full run: 0 unaccounted; outcomes split into confirmed success / confirmed failure / verified compensation / escalated unresolved / pending | invariant checks closing every fault test; scoring in `chaos/scoring.py`, `chaos/oracle.py` |
| **A6** Compensation history and uncertainty remain visible | Met | Phase C exit (A6); [workflow outcomes](demo/workflow-outcomes.md); full run: compensation effects counted separately (SafeAct 0 compensation duplicates) | `test_compensation.test_confirmed_order_failure_refunds_exactly_the_charge`, F13 tests; console `ui/e2e/workflows.spec.ts` |
| **A7** Tenant isolation and human-only approval at API and MCP boundaries | Met | [Milestone 3 exit (API side)](phase-d/milestone-3-exit-evidence.md), [API authorization review](phase-d/api-authorization-review.md), [Milestone 4 exit (MCP)](phase-d/milestone-4-exit-evidence.md), [MCP authorization review](phase-d/mcp-authorization-review.md) | `tests/integration/test_api_review.py`, `test_mcp_authorization.py`, `tests/isolated/`; qualification F14 and F15 (0 failures); `tests/e2e_api/test_walkthrough.py` (agent has no approval tool, ATTEST_APPLIED refused, stale approval 409) |
| **A8** Three full-stack journeys pass under denied access, stale state and reconnection | Met | [Milestone 3 exit, A8](phase-d/milestone-3-exit-evidence.md#a8--under-denied-access-stale-state-and-reconnection) | `tests/e2e_api/test_journeys.py`; Playwright `ui/e2e/journeys.spec.ts`, `negative.spec.ts`, `a11y.spec.ts` |
| **A9** Real MCP-client trials and a real sandbox adapter, reproducible | Met, narrowed (one model snapshot; 8 Stripe scenarios × 2, no injected 429/outage at Stripe) | [Agent evaluation](phase-d/agent-eval-v1/README.md); [Stripe sandbox review](phase-d/stripe-sandbox-v1/REVIEW-M58.md) (16/16, 77/150 calls) | `tests/e2e_agent/test_agent_harness.py`; `deploy.sandbox.run_stripe --fake` in CI; frozen `deploy/sandbox/manifest_stripe_v1.json` |
| **A10** Baseline comparisons, failure seeds, actual interleavings and costs published | Met | [Full run](phase-e/qual-v1/full/REVIEW.md): all 6,000 scored rows (`results.jsonl`), 700 failure rows, traces of every failing/unstable row, coverage, environment, $0 cost; [B3 design](phase-e/b3-design.md); raw output digests in `outside-repo.json` | `chaos/baselines/`; Phase A baseline tests |
| **A11** CI runs focused regression/integration checks; separate reproducible qualification command | Met | `.github/workflows/ci.yml` passed (private development history: PR #7, run 40252297269, commit `26e9761`; all four jobs); `chaos.qualify` and the manual `qualification.yml` | Phase A, PostgreSQL + broker, isolated security and console jobs; full local runs per sub-step (M5.13–M5.15) |
| **A12** Fresh-clone setup, architecture limits and demo verified | Met (`fresh-clone.yml` has passed on GitHub on every merge to main since Milestone 5, private development history; locally warm and cold) | [Compose and fresh-clone review](phase-e/compose-v1/REVIEW.md) (warm and cold PASS); README limits; [demo walkthrough](demo-walkthrough.md) verified on the Compose stack | `deploy/fresh_clone_check.sh`; `tests/phase_a/test_compose_stack.py`; `tests/e2e_api/test_walkthrough.py` |

## §12 three-minute demonstration

| Demo item | Where | Verified by |
|---|---|---|
| A refund with timeout-after-success: the timeline shows the uncertainty, then verified completion with no second dispatch | [Walkthrough, beat 1](demo-walkthrough.md#1-timeout-after-success-verified-not-resent-about-40-s); optional Stripe test-mode version in the same document | `tests/e2e_api/test_walkthrough.py` (one attempt; UNKNOWN in the path; one ledger effect); `tests/e2e_api/test_stripe_demo.py` (one POST, one refund at the Stripe fake) |
| Late commit with an empty lookup: SafeAct stays unresolved | [Walkthrough, beat 2](demo-walkthrough.md#2-late-commit-empty-lookup-unresolved-on-purpose-about-40-s) | same test: ESCALATED_UNRESOLVED with one ledger effect (the order exists; SafeAct did not guess) |
| Agent self-approval and a stale human approval rejected; a permitted human evidence path succeeds | [Walkthrough, beat 3](demo-walkthrough.md#3-no-self-approval-no-stale-approval-then-the-permitted-path-about-70-s) | same test: no approval tool, ATTEST_APPLIED refused, 409 `stale_approval`, reviewer approval leads to SUCCEEDED `human_attested`; also checked in a browser on the Compose stack |
| The comparison report, with the robust baseline, unresolved rates and known limits | [Walkthrough, beat 4](demo-walkthrough.md#4-the-comparison-report-about-30-s); [README results](../README.md#results) | figures come from `docs/phase-e/qual-v1/full/summary.json` |

There is no demo video (author decision, 2026-10-27): the README shows screenshots of
the guided walkthrough (`docs/images/`), and the walkthrough itself runs locally.

## §12 required evidence

| Item | Evidence |
|---|---|
| Architecture and connector contracts | [README](../README.md#how-it-works); `core/connector_profiles.py`; [Stripe contract](phase-d/stripe-sandbox-contract.md); [F16 limits](phase-d/f16-limits.md) |
| Schema and state-machine definitions | `core/lifecycle.py`; `storage/lifecycle_sql/v0003.py` to `v0022.py` (frozen, golden digests); [durable execution contract](phase-b/durable-execution-safety-contract-v0.md); [projection](phase-e/projection.md) |
| Fault catalog | `chaos/fault_contracts.py`, `chaos/scenarios.py`, `chaos/combinations.json`; coverage list `chaos/coverage_qual_v1.json` |
| Versioned test scenarios | `chaos/scenarios_qual_v1.jsonl` (1,200, seed 510112026) pinned by `chaos/manifest_qual_v1.json` (`sha256: b779fa72…`); agent scenarios `deploy/evaluation/agent_v1/scenarios_heldout.jsonl` |
| Baseline implementation details | `chaos/baselines/`; [B3 design](phase-e/b3-design.md); [weaker baselines](phase-e/weaker-baseline-implementation-plan.md); [STOP 2 review](phase-e/stop2-post-pooling-20261024/REVIEW.md) |
| Raw outcomes | `docs/phase-e/qual-v1/full/results.jsonl` (all scored rows), `failure-rows.jsonl`, `traces-failing-unstable.jsonl`; full raw output by digest (`outside-repo.json`, Release asset planned); load: `docs/phase-e/load-v1/requests.jsonl.gz` |
| A reproduction command | README [Evidence and reproduction](../README.md#evidence-and-reproduction); each review states its command |
| Agent prompts and tool versions | `mcp_server/agent/prompts/v1/system.md` (`agent-prompt-v1`, its sha256 in the manifests); toolset digests `safeact-guarded-v2` and `direct-mock-v1` in `deploy/evaluation/agent_v1/manifest_heldout.json`; SDK versions in the same manifest |
| Sandbox results | [Stripe sandbox v1](phase-d/stripe-sandbox-v1/REVIEW-M58.md) |
| Performance measurements | [Load test v1](phase-e/load-v1/REVIEW.md); recovery latency distributions in the full run's `summary.json` (below) |
| Security boundary tests | `tests/isolated/` (PostgreSQL identity isolation, secret logging); `tests/integration/test_lifecycle_privileges.py`, `test_admission_role_policy.py`, `test_atomic_gate_security.py`, `test_mcp_authorization.py`, `test_api_sessions.py`; [observability](phase-e/observability.md) (no identifiers in metrics) |
| A concise failure analysis | [Full run: SafeAct's 13 missed completions](phase-e/qual-v1/full/REVIEW.md#safeacts-13-missed-completions-6-rows-the-declared-trade-off); [diagnostic of the baseline failures](phase-e/qual-v1/diagnostics/REVIEW.md); [observations and defects](phase-e/observations.md) |

## §12 result fields

**Safety outcomes:** duplicate and orphan effects by scenario and connector. The full
run reports them per arm, per family and per connector. SafeAct had 0 safety-failure
rows on every connector ([REVIEW](phase-e/qual-v1/full/REVIEW.md)), and every row is in
`results.jsonl`.

**Business completion,** from SafeAct's operation outcomes in the full run:

| Confirmed success | Confirmed failure | Verified compensation | Escalated unresolved | Pending at scoring |
|---:|---:|---:|---:|---:|
| 911 | 67 | 22 | 468 | 13 |

Useful completion was 924/937 achievable units. Escalation is reported, never counted as
completion.

**Recovery performance,** SafeAct in the full run (seconds, nearest-rank percentiles,
measured under time-scaled contracts):

| Measure | Count | p50 | p95 | Max |
|---|---:|---:|---:|---:|
| Time to confirmed resolution | 1,000 | 2.24 | 13.75 | 40.32 |
| Time to escalation | 627 | 8.44 | 11.79 | 17.33 |
| Queue wait | 1,452 | 0.22 | 4.06 | 38.19 |
| Verification delay | 497 | 1.19 | 11.99 | 12.90 |

Hardware and workload are in `environment.json` and the review. Load-test latencies and
throughput are in [load-v1](phase-e/load-v1/REVIEW.md).

**Agent and product value:** these are measured tool behaviour only
([agent evaluation](phase-d/agent-eval-v1/README.md)). No users, time savings or
adoption are claimed.

## §13 definition of done

| Bullet | Status | Evidence |
|---|---|---|
| All required acceptance checks have linked evidence; a failing check narrows the claim or blocks release | Met, with narrowings below | the A1–A12 table above |
| Mocks and real sandbox behaviour are identified separately; the independent ledger cannot leak into connector logic | Met | README [connector assumptions](../README.md#connector-assumptions); the ledger is only on the harness admin port (`connectors/http_mock/admin_api.py`), never a lookup; Stripe ground truth is Stripe's own refund list |
| Results contain observed values with workload, versions and reproduction instructions | Met | every review records its command, machine, versions (`environment.json`) and digests |
| The application is usable from a fresh clone; the reviewer can inspect uncertainty and denied actions | Met locally | fresh-clone check PASS (warm and cold); the walkthrough shows UNKNOWN, escalation, the refused agent attestation and the stale approval |
| The README states connector assumptions, unsupported faults, unresolved cases, and local coordination versus remote guarantees | Met | [README](../README.md) sections of those names |

## Narrowed claims and open items

- **Scope of the evidence:** declared contracts, mock providers and one laptop.
  Stripe-specific evidence covers 8 scenarios × 2 in test mode, with no 429, outage or
  retention expiry exercised at Stripe.
- **GitHub CI** passed (private development history: PR #7, run 40252297269, commit `26e9761`). Its first
  three runs failed on test defects that only the slower runner exposed; all were fixed in
  the tests ([observations](phase-e/observations.md)).
  - `fresh-clone.yml` runs on a push to main and has passed on GitHub on every merge to main
    since Milestone 5 (private development history).
  - `qualification.yml` is manual and gated.
  - The optional full qualification run in CI (neutral hardware) is planned, not run.
- **The full raw output** is committed by digest only. Its Release asset is planned,
  not yet published.
- **The optional Stripe test-mode demo profile** was run once against Stripe test
  mode: the normal refund, and the dropped response with exactly one refund at Stripe
  ([stripe-demo-v1](phase-e/stripe-demo-v1/REVIEW.md)). Here "Succeeded" means Stripe
  accepted the request and created the refund. Both refunds reported the status
  `succeeded` at response time. Settlement to the customer is out of scope. This is one run of two cases,
  not a qualification.
- **Retained results:** the F16 trade-off (13 missed completions), F09 not completable,
  and relay republishing under backlog (load test).
- **Open:** worker-side identity binding; runtime error-log attribution; SQL
  canonical-JSON enforcement; runtime credential/subject binding; agents get one
  operation per target; asynchronous Stripe refund failure is out of scope.
- **No demo video** (author decision, 2026-10-27): README screenshots and the guided
  walkthrough instead.
