# SafeAct — Project Instructions for Claude Code

## What this project is
SafeAct is an execution-safety layer for AI-agent actions. It prevents an agent from
executing an irreversible action twice, acting without authorization, or making an
unsafe recovery decision after a crash.

Goal: a portfolio-grade system that proves safe external-action execution end to end,
not a collection of isolated backend tests.

## Core principles (never violate)
- Persist the action durably BEFORE calling any external system.
- UNKNOWN is a legitimate state. An empty provider lookup is NOT proof of failure.
- Never claim universal exactly-once. Target: zero observed prohibited duplicate
  effects within the declared tested contracts.
- Keep the deny-only production boundary. Production execution is not approved.
- Do not loosen existing DB check constraints to fake a completed state machine.
- The AI agent must never approve its own restricted action, invent verification
  evidence, or force a retry against policy.
- Never silently drop original-scope requirements. Any reduction must be labeled as
  a smaller release (e.g. "v1").

## Tech stack
Python, FastAPI, Pydantic · PostgreSQL, SQLAlchemy, Alembic · Celery + Redis ·
React + TypeScript · MCP + tool-calling model · Docker Compose · GitHub Actions ·
Prometheus / structured logs. No Kubernetes, Terraform, or GPU serving.

## Durable lifecycle states
RECORDED → CLAIMED → DISPATCHING → SUCCEEDED | FAILED_CONFIRMED | UNKNOWN
UNKNOWN → VERIFYING → SUCCEEDED | FAILED_CONFIRMED | RETRY_ELIGIBLE | ESCALATED_UNRESOLVED
Enforce legal transitions, worker ownership, version checks, and durable audit evidence.
- Deliberate deviation from the blueprint §05 diagram: VERIFYING → FAILED_CONFIRMED is
  legal (needed for §09 confirmed permanent failure and fault F02). Any move to
  FAILED_CONFIRMED requires authoritative non-application evidence for every attempt and
  no applied evidence, enforced inside the protected SQL append function (SQLSTATE SAT09).
  An empty, NOT_FOUND or INCONCLUSIVE lookup can never reach it. SUCCEEDED likewise
  requires recorded applied-effect evidence.
- Deviation (0005): ESCALATED_UNRESOLVED -> VERIFYING (reopen). Escalation notifies an operator;
  it is not abandonment. Only new decisive evidence recorded after that escalation may reopen,
  within the contract's max_reopens, and a reopened operation cannot "verify later".
- Deviation (0006): CLAIMED -> RETRY_ELIGIBLE / CLAIMED -> UNKNOWN. Nothing is dispatched while
  CLAIMED (attempt + DISPATCHING commit together), so an abandoned claim can leave safely.
- Retry (0007): safeact_retry_basis is the one SQL definition of a protected retry: proof of
  non-application for every earlier attempt, or a native key (binds params, concurrent-safe)
  before first fence + retention - margin. Checked on VERIFYING -> RETRY_ELIGIBLE and again at
  the fence (SAT12); attempt budget and a cap of 3 abandoned claims (SAT13). DISPATCHING ->
  UNKNOWN reasons are a fixed vocabulary (core.lifecycle.AMBIGUOUS_REASONS / RETRY_PENDING_REASON).
- Deviation (0008): RETRY_ELIGIBLE -> ESCALATED_UNRESOLVED, only when safeact_reclaim_blocked()
  says reclaim is blocked (abandoned_claim_limit or attempt_budget_exhausted), with that reason.
  Retry work that can never run goes to human review instead of being parked.
- Provider keys (0008): derived server-side from operation identity (core/provider_keys.py);
  a caller can never choose one. A CHECK recomputes the derivation; unique index on
  (provider_account_id, provider_idempotency_key). Evidence recorded after an escalation reopens
  it without a provider call (safeact_reopen_evidence), within max_reopens.
- Deviation (0012): RECORDED -> CANCELLED_BEFORE_CLAIM, a new terminal state reached only through
  safeact_api_cancel: first transition, no claim or attempt, under the cancellation's own id (the
  append function enforces it). Claim/relay/fence already exclude it; accounting keeps it in its own
  bucket (A5); workflow steps are never cancelled and safeact_workflow_status_of reads a cancelled
  step as INVALID. Any later cancel request is recorded (CANCELLATION_REQUEST_REFUSED), never undoes.
- Product admission (0012): safeact_api_submit / _create_workflow derive tenant and actor from the
  session and require exact action grants (SAT21); same key = same operation, changed request = SAT22;
  invalid = SAT23. Reads are tenant-scoped (requesters see their own; investigators/reviewers the
  tenant); anything else is 404. The API picks the connector and canonicalizes the payload
  (api/catalog.py); duplicate JSON keys are refused.
- Review (0013): an investigator proposes REVERIFY, PROTECTED_RETRY or ATTEST_APPLIED; the server
  checks eligibility and binds seq, state, payload hash, evidence digest, contract, action
  (+reference) and expiry into a binding digest. A human reviewer (never the proposer or requester:
  SAT25) decides once (SAT24; same decision_key replays). Stale/expired bindings are a recorded result
  (APPROVAL_REFUSED_STALE, HTTP 409), not an error. PROTECTED_RETRY: approval itself moves
  VERIFYING -> RETRY_ELIGIBLE; contracts in safeact_contract_review_policies need it (append guard
  SAT27); the reconciler waits without lookups until the investigation deadline, then decides without
  a retry. REVERIFY: one extra post-escalation lookup beyond interval/horizon (safeact_take_reverify).
  ATTEST_APPLIED: confirmed_applied evidence with provenance human_attested; a CHECK allows human
  provenance only for applied effects; successes are reported by provenance.
- Review follow-ups (0015): reason retry_approval_expired = FAILED_CONFIRMED because no reviewer
  approved a required retry before the deadline (append guard: reviewed contract, deadline passed,
  no live approval, system-provenance proof for every attempt). POST /actions/{id}/verification-requests:
  investigator, no approval, lookup-only within the contract interval/horizon and 3 per escalation;
  beyond bounds needs a REVERIFY proposal. The review policy row is part of the binding digest.
  idle_in_transaction_session_timeout=60s on every application login role (bounds feed delay).
- Change feed (0014): safeact_audit_events.commit_xid (xid8, defaulted) + safeact_api_changes(secret,
  after): operations with after <= commit_xid < xmin(current snapshot), tenant-visible; late commits
  are delayed, never skipped. GET /events (api/events.py) streams them as SSE with the horizon as id;
  Last-Event-ID catch-up, reset/truncated -> re-read REST, session re-checked every poll
  (session_ended), max lifetime then "reconnect", per-session stream cap. NEW_COLUMNS in the head
  view keeps autogenerate from dropping commit_xid.
- Identity (0011): principals, bcrypt-12 credentials, role/action grants, hash-only sessions
  (8 h absolute, 30 min idle, revocation rows) verified in SQL. safeact_api_login never raises on
  bad credentials (returns ok=false so the failure row commits); one bcrypt check on every path
  (dummy hash for unknown users); 5 failures/15 min lock the account, 20 lock the source. The API
  role may EXECUTE only safeact_api_* functions and never logs statements or parameters
  (storage/identity_admin.NO_SECRET_LOGGING). SQLSTATEs SAT17 unauthenticated, SAT18 CSRF, SAT19
  role, SAT20 identity invalid. Internal helpers are SECURITY INVOKER (tests enforce it).
- Agent keys (0016): service principals authenticate to the MCP boundary with admin-issued keys
  ('sak_'+64 hex, SHA-256 only; scopes subset of {submit, investigate}, CHECK; <= 30 days; append-only
  revocations; storage/identity_admin.issue_agent_key). The MCP login role may EXECUTE only
  safeact_mcp_*; each resolves the key in SQL (safeact_agent_call), mints a one-minute session that
  never leaves the transaction, and calls the existing safeact_api_* function. safeact_api_session
  (replaced) re-checks a key-linked session and narrows roles to its scope; a service principal has
  no other session. safeact_api_visible (replaced): a service principal sees only what it requested.
  Agents never pass a request key (derived from principal, target and intent_ref) and get SAT28 for a
  second operation on a target whose earlier one is not FAILED_CONFIRMED or CANCELLED_BEFORE_CLAIM
  (agent path only; known limit: no second distinct action on one target, e.g. two partial refunds).
  Agents may propose REVERIFY / PROTECTED_RETRY only; there is no decision function for them.
- Projection (0017): safeact_state_projection (one row per operation = latest transition, RECORDED
  at seq 0) is derived from the log and written only in the protected path, same transaction: an
  AFTER INSERT trigger on safeact_operations (SECURITY DEFINER, lsql.TRIGGER_FUNCTIONS, never
  granted) seeds it, safeact_lifecycle_append advances it. A guard trigger refuses any write that
  does not equal the next log row, even from the superuser or owner (SAT29); the lock refuses when
  projection seq != log seq (SAT29). The 0003 view keeps its columns and reads it; claimable / relay /
  in-state scans use its (state, operation_id) index. storage/state_projection.projection_drift is
  checked before every disposable schema is dropped (fixtures, API E2E, agent harness); fixtures that
  write the log directly call catch_up_projection.
- F16 limits (0018), opt-in per contract via safeact_contract_limits (v1 contracts: none; v2 mock
  contracts in core.connector_profiles.LIMITED_PROFILES; stripe-test): every move to RETRY_ELIGIBLE
  records a deterministic jittered capped backoff not_before (>= Retry-After fact) and an early reclaim
  is refused (SAT30); a per provider-account breaker (failing dispatch outcomes, one half-open probe)
  and token bucket refuse claims (SAT31) without appending anything; UNKNOWN -> VERIFYING (one lookup)
  is paced and refused early (SAT30); 'ambiguous_rate_limited' joins the vocabulary; safeact_pacing
  for the reconciler/console. Limits sit at claim, not at the fence (a fence refusal would burn the
  abandoned-claim cap). Details: docs/phase-d/f16-limits.md.
- Stripe sandbox (M5.3): connector stripe-test (raw HTTPS via httpx2, no SDK, pinned
  Stripe-Version 2026-09-30.endive; contract and classification in docs/phase-d/stripe-sandbox-contract.md).
  Key: restricted rk_test_ only, in ~/.config/safeact/stripe.key (mode 600, outside the repo, created by the
  author; Claude never reads, prints or logs it); every other Stripe key kind refused by prefix; a real client
  needs SAFEACT_ALLOW_STRIPE_SANDBOX=1 and never runs with CI/GITHUB_ACTIONS set; any livemode object
  stops the run. CI exercises the runner only with --fake (connectors/stripe_fake.py).
- Frozen means a migration's rendered SQL never changes. The only allowed edit to a frozen module is
  adding an exact schema name to the allowlist, proven by unchanged golden digests. (Done once:
  "safeact_demo" in v0003._schema; tests/phase_a/test_schema_allowlist.py.)
- Source of truth: core/lifecycle.py (states, legal pairs); each migration's frozen SQL module in
  storage/lifecycle_sql/ seeds its pairs, and a test proves their union equals core. storage/schema.py does not describe
  0003+ objects;
  the Alembic include_object guard keeps autogenerate from proposing to drop them.

## Connector profiles
- Payments: native idempotency, evidence lookup, retention window, refund scenario.
- CRM: no guaranteed idempotency; delayed/incomplete lookup may leave it unresolved.
- Email: irreversible, no reliable dedup; ambiguous dispatch must never be blindly repeated.

## Commands
Run from the repo root with `.venv/bin/python` (not conda base). Tests use unittest, not pytest.
- Start dev PostgreSQL and Redis (reads `.env`, which must be chmod 600; Redis needs
  SAFEACT_DEV_REDIS_PORT / SAFEACT_DEV_REDIS_PASSWORD): `docker compose --env-file .env -f deploy/compose.yaml up -d`
- Python deps: requirements-db.lock, requirements-broker.lock (Celery 5.5.3 over Redis).
- Run Phase A tests: `.venv/bin/python -m unittest discover -s tests/phase_a -t . -p 'test_*.py'`
- Run PostgreSQL + broker tests: `SAFEACT_RUN_DB_INTEGRATION=1 SAFEACT_RUN_DISPOSABLE_DB_TESTS=1 SAFEACT_RUN_DB_SECURITY_TESTS=1 SAFEACT_RUN_BROKER_TESTS=1 SAFEACT_RUN_ISOLATED_REDIS_TESTS=1 .venv/bin/python -m unittest discover -s tests/integration -t . -p 'test_*.py'`
  (broker/process suites use reserved Redis test databases 9–15; isolated
  Redis tests create and remove their own container and preserve the shared server).
- Run isolated security tests (21; needs local PostgreSQL 14.23 `initdb`/`pg_ctl`/`postgres` on PATH, non-root):
  `SAFEACT_RUN_ISOLATED_CLUSTER_TESTS=1 .venv/bin/python -m unittest discover -s tests/isolated -t . -p 'test_*.py'`
- Rebuild the local demo environment (DEMO ONLY; idempotent, drops and recreates safeact_demo):
  `.venv/bin/python -m deploy.demo.bootstrap`
- Run the whole demo stack against it (API on :8000, guarded MCP server on 127.0.0.1:8002/mcp, relay,
  Celery worker, reconciler, advancer, mock providers with a harness-only fault script):
  `.venv/bin/python -m deploy.demo.run_stack` (agents use the demo-*-agent keys in .demo-credentials
  as `Authorization: Bearer sak_...`; MCP only: `.venv/bin/python -m mcp_server.server`)
  (API only: `.venv/bin/python -m uvicorn --factory api.app:demo_app`)
- Same stack as separate OS processes (M5.2: real prefork Celery worker, relay/reconciler/advancer
  loops, API, MCP, HTTP mock providers on 8101-8103 + harness-only admin/ledger API on 8109; logs in
  .stack-logs/): `.venv/bin/python -m deploy.demo.run_stack --processes` (deploy/stack/; each process
  reads roles from SAFEACT_STACK_CREDENTIALS, default .demo-credentials; the worker forces billiard's
  fork start method because macOS defaults to spawn, which loses the task registration).
- Run API end-to-end journeys (real uvicorn + Celery over Redis DB 14; flushes only DB 14):
  `SAFEACT_RUN_API_E2E=1 .venv/bin/python -m unittest discover -s tests/e2e_api -t . -p 'test_*.py'`
- Python deps also include requirements-obs.lock (prometheus-client; every job),
  requirements-test.lock (hypothesis, integration tests only) and
  requirements-api.lock (FastAPI, Pydantic, uvicorn; httpx2 2.13.1 for tests)
  and requirements-mcp.lock (official MCP SDK mcp==2.3.0, installed on top of the API lock).
- Console (ui/, Node 24, pinned exact versions): `cd ui && npm ci`, then `npm run dev` (Vite on
  :5173, same-origin proxy to the API on :8000; HTML navigations get the app), `npm test` (Vitest),
  `npm run typecheck`, `npm run lint`, `npm run build`. After an API change:
  `.venv/bin/python -m deploy.demo.export_openapi` then `npm run gen:api` (CI checks both).
- Console browser journeys (Playwright + axe; starts deploy/demo/e2e_server.py on :8001 with Redis
  DB 13 and Vite on :5173): `cd ui && npx playwright install chromium && npx playwright test`
  (each run keeps artifacts in ui/test-results/<run>/ plus <run>-context.json; the e2e server refuses
  to start if Redis DB 13 has another client or :8001 is taken)
- Stripe sandbox runner: offline `.venv/bin/python -m deploy.sandbox.run_stripe --fake --out /tmp/stripe`;
  real (only after the author's go, key file in place):
  `SAFEACT_ALLOW_STRIPE_SANDBOX=1 .venv/bin/python -m deploy.sandbox.run_stripe --out docs/phase-d/stripe-sandbox-v1/run-1`
- Run development evaluator: `.venv/bin/python -m deploy.evaluation.run_corpus_v1`
- Agent evaluation (mock providers only): scripted, free:
  `.venv/bin/python -m deploy.evaluation.agent_v1.run --split dev --model scripted-honest --out /tmp/eval`;
  harness tests: `SAFEACT_RUN_AGENT_HARNESS=1 .venv/bin/python -m unittest discover -s tests/e2e_agent -t . -p 'test_*.py'`.
  Paid runs only after Gate 2 (SAFEACT_ALLOW_PAID_MODEL=1, key file ~/.config/safeact/openai.key mode
  600, never read by Claude, frozen --manifest, $4 total cap). Python deps also include
  requirements-agent.lock (openai==3.24.0).
- CI: .github/workflows/ci.yml runs Phase A, PostgreSQL + broker (docker-run postgres/redis
  containers, throwaway credentials, alembic head + provisioning) and the isolated suite (exact
  PGDG postgresql-14=14.23-1.pgdg24.04+1) on pushes to main, on pull requests and on manual
  workflow_dispatch (since M4.0; one concurrency group per PR cancels superseded runs, never on
  main). Actions pinned to commit SHAs;
  `gh run watch` to follow a run.
- Measure lifecycle scan timings (known limit): `SAFEACT_RUN_DB_INTEGRATION=1 SAFEACT_RUN_DISPOSABLE_DB_TESTS=1 .venv/bin/python -m deploy.evaluation.measure_lifecycle_scans`

## Current status (UPDATE AFTER EVERY STEP)
- Author instruction (2026-10-24): integrate validated connection pooling and
  run regression/local timing checks. Do not run pre-run, frozen smoke,
  calibration or full qualification; the author will explicitly authorize
  those later. No staging or commits. Implementation and current diagnostics:
  docs/phase-e/pooling-integration-plan.md. The earlier STOP 2 source review
  describes the pre-pooling runtime and must be superseded after validation.
  The exact initial tree matched its sealed 1626 Python / 51 console baseline.
  Integration is complete: bounded native pools in API, worker,
  relay/recovery/advancer scopes, threaded demo and common SafeAct admission.
  Shared API lifespan ownership, automatic-preparation cache survival and a
  reproduced shutdown/reset race are covered by new regressions. Final checks
  pass: 1650 Python / 51 console, typecheck/lint/build. The final 710-test
  integration discovery took 1315.863 seconds with zero skips. A local check
  through the integrated execution path measured median 96.79 -> 44.67 ms at
  concurrency 1 and 131.01 -> 67.31 ms at concurrency 4, with 400 measured
  operations plus 128 warmups and zero errors/duplicate effects. These exclude
  public API/queue time and are not production capacity or cross-arm results.
  Source-only candidate amendment: 231 backend files; all corpus, contract,
  timing, coverage/applicability and scoring inputs unchanged. Evidence:
  docs/phase-e/dev-pooling-integration-20261024/REVIEW.md. The earlier STOP 2
  packet is historical; no pre-run, frozen smoke/calibration/full execution
  has started. Wait for the author's explicit instruction before pre-run.
- Author instruction (2026-10-22): do not commit. Keep this work uncommitted
  until the author changes that instruction.
- Author instruction (2026-10-23): proceed with the remaining implementation
  and development checks; the next approval checkpoint is STOP 2 — PRE-RUN
  REVIEW. Announce it and pause there. Execution plan:
  docs/phase-e/pre-stop2-implementation-plan.md. Final-run authorization remains
  required; this continuation does not authorize frozen qualification.
- Final qualification gate (author, 2026-10-22): announce **FINAL BIG
  QUALIFICATION RUN** prominently and wait for the author's explicit go.
  General instructions to resume improvements do not authorize that run.
  STOP 2 and the existing corpus/scoring freeze remain prerequisites.
- Branch: feature/m5-sandbox-qualification (PR #6 merged to main by the author)
- Last clean commit: see `git log -1` (M5.8: Stripe sandbox evidence and regression fixes)
- Tests: current regressions reviewed: Python 1650 (908 Phase A, 710 PostgreSQL + broker,
  21 security, 9 API E2E, 2 agent harness), console 51 (37 Vitest, 14 Playwright),
  typecheck/lint/build and diff check passed (2026-10-24). Final integration
  discovery passed all 710 in 1315.863 seconds with no skips, including isolated
  Redis and the registered B0–B3 adapter tests. Current evidence:
  docs/phase-e/dev-pooling-integration-20261024/REVIEW.md. The earlier adapter
  packet remains historical in docs/phase-e/dev-baseline-adapter-20261023/.
  The owned-stack
  packet remains historical in docs/phase-e/dev-baseline-owned-stack-20261023/.
  The
  process observation packet remains historical in docs/phase-e/dev-baseline-process-trace-20261023/.
  observer packet remains historical in docs/phase-e/dev-baseline-observer-20261023/.
  workflow/review packet remains historical in docs/phase-e/dev-baseline-workflow-review-20261023/.
  execution packet remains historical in docs/phase-e/dev-baseline-execution-20261023/.
  The earlier
  boundary packet remains historical in docs/phase-e/dev-boundaries-20261022/.
  Customer-support
  evidence remains historical in docs/demo/validation-20261022/REVIEW.md.
  The earlier F13 regression's concurrent-login timeout and unchanged passing
  reruns remain in docs/phase-e/dev-workflows-20261022/; no timeout was relaxed.
  Defects found and items under observation in M5: docs/phase-e/observations.md (an intermittent
  agent-harness start-up race, fixed in M5.3: embedded uvicorn now uses log_config=None).
  One local Playwright run on 2026-10-20 failed 3 specs right after the Python suites; reruns
  passed (observation in docs/phase-d/mcp-authorization-review.md).
- Dev evaluator: 4 PASS / 6 NOT_RUN (SA-EVAL-003 NOT_RUN)
- Dev DB: at 0022_admission_race, no execution history, no principals, no contracts registered
  in public (backup: ~/safeact-backups/safeact_dev-pre-0004-20261019T021225.dump). After each
  migration: alembic upgrade head, then re-run deploy/security/provision_local_admission_role.sql
  (psql -1); the script and test_admission_role_policy pin the head revision. Then rebuild the demo.
- Last completed: Milestone 3 (Phase D part 1), D0 -> D10 + review follow-ups (0015). Exit evidence
  with journeys, A7 (API), A8, F14, deviations and limits: docs/phase-d/milestone-3-exit-evidence.md;
  authorization review: docs/phase-d/api-authorization-review.md. Milestone 2 evidence:
  docs/phase-c/phase-c-exit-evidence.md.
  Migrations 0004-0016; frozen SQL per revision in storage/lifecycle_sql/vNNNN.py (rendered SQL
  never changes; golden digests; see the frozen rule above). Broker: Celery 5.5.3 over Redis 7.4 (compose).
- In progress: Milestone 3 (approved plan D0-D10: CI; API skeleton + safeact_demo bootstrap;
  DB-verified sessions 0011; authorized submission/workflow/cancel 0012; version-bound review
  0013; change feed + SSE 0014; API E2E journeys -> STOP for review before React UI).
  D0 done: CI workflow (verified green). D1 done: api/ (FastAPI factory, /healthz, /readyz,
  error mapping, JSON logs with redaction, security headers), storage/lifecycle_environment.py
  (shared schema builder; the test fixture uses it), deploy/demo/bootstrap.py (one idempotent
  rebuild of safeact_demo; .demo-credentials mode 600). run_stack.py moved to D6 (first use).
  D2 done: 0011 identity/sessions, storage/product_api.py, api/auth.py (/auth/login, /logout, /me;
  __Host- Secure SameSite=Strict cookies, CSRF header checked in SQL, cross-site write guard),
  demo accounts seeded by the bootstrap (7 sign-ins + 1 service principal; tenants north/south),
  isolated PG 14.23 log-grep test with a loud-role control. D3 done: 0012 (submit, workflow creation bound to its requester, tenant-scoped
  reads, my grants, cancellation), api/actions.py + api/catalog.py, demo requester grants. D4 done: 0013 proposals/decisions, api/review.py, reconciler approval wait +
  REVERIFY consumption, provenance in accounting and the detail view. D5 done: 0014 change feed,
  GET /events SSE. D6 done: deploy/demo/run_stack.py,
  deploy/demo/profiles.py (demo/e2e time-scaled contracts; bootstrap registers demo pace),
  tests/e2e_api (8 journeys), docs/phase-d/api-authorization-review.md.
  Review done (decisions applied in 0015, recorded in the review doc). D7+D8 done: ui/ console
  (client, session, SSE hook, accessible layout, sign-in, submit/track, investigate, review).
  D9 done: ui/e2e (9 browser specs: 3 journeys, stale screen, denied tenant, duplicate
  submission, connection interruption, axe + keyboard). D10 done: exit evidence
  docs/phase-d/milestone-3-exit-evidence.md; README demo instructions. MILESTONE 3 COMPLETE.
- In progress: Milestone 4 - MCP server + bounded agent (approved plan M4.0-M4.7; STOP for review
  after M4.3, Gate 2 before any paid model call; $4 total cap for paid runs). M4.0 done: CI runs on
  push to main, pull_request and workflow_dispatch, one group per PR; known item (unreproduced
  ProvenanceGuardTests setUpClass error) recorded in milestone-3-exit-evidence.md; verified on draft
  PR #6 (one pull_request check set per push, superseded run cancelled). M4.1 done: 0016_agent_keys
  (see "Agent keys (0016)" above), storage/agent_api.py, MCP role in bootstrap.Environment and the
  grant wiring (mcps=), demo agents north/south with keys in .demo-credentials, tests
  test_agent_keys, test_migration_0016_roundtrip, isolated test_mcp_secret_logging.
  M4.2 done: mcp_server/ (server.py low-level SDK server, closed schemas validated in-process,
  unknown tools refused, BearerRequired format gate, tools.py pinned toolset safeact-guarded-v1,
  untrusted.py authoritative vs untrusted_provider_text, settings.py loopback only), run_stack MCP
  on :8002, requirements-mcp.lock, key-shaped log redaction. M4.3 done:
  tests/integration/test_mcp_authorization.py (A7 at MCP, F15) and
  docs/phase-d/mcp-authorization-review.md. Reviewed 2026-10-20 (follow-ups applied: one parse per
  request proven, per-run browser artifacts + e2e preflight).
  M4.4 done: deploy/evaluation/agent_v1 (scenarios dev 8 / held-out 12, pinned; faults.py one plan
  for both arms; profiles.py incl. truthful v1-short-retention for F09; harness.py; scoring.py),
  mcp_server/direct (mock-only direct tools). M4.5 done: mcp_server/agent (runner, models with
  OpenAIModel + PolicyModel, budget.py with $4 total cap in spend.jsonl, secrets.py key file outside
  the repo), run.py, report.py, secret_scan.py, tests/e2e_agent (SAFEACT_RUN_AGENT_HARNESS=1, Redis
  DB 12, scripted models; in CI), docs/phase-d/agent-evaluation-design.md.
  Gate 2 approved 2026-10-20: gpt-4.1-mini-2025-04-14 (deprecated 2026-10-14, shutdown 2027-01-14,
  replacement gpt-6-luna; recorded in manifests and reports), $0.04/trial, 16 dev + 72 held-out.
  M4.6 done: dev calibrations x3 and the frozen held-out run (trials 1-3) plus the pre-registered
  extension (trials 4-6): combined 72 trials/arm: guarded 71/72 (v1 66), 0 unsafe executed; direct
  39/72, 28 unsafe executed, 12 false claims, 6 orphan effects; deterministic 72/72. Total spend
  $0.3918 of $4. Disclosures: tools v2, scoring v2, effect attribution fix (all before held-out).
  M4.7 done: docs/phase-d/milestone-4-exit-evidence.md, README results table with scope, PR #6
  marked ready for review (merge is the author's). MILESTONE 4 COMPLETE.
- In progress: Milestone 5 (approved plan M5.1-M5.15, ~/.claude/plans/pasted-content-id-207e-read-claude-md-wild-hennessy.md;
  decisions in memory m5-plan-decisions). STOP 1 after M5.3 (before the user creates the Stripe
  rk_test_ key); STOP 2 after M5.11 (baselines + corpus frozen, before the full run). Everything that
  changes the system under test lands before the freeze.
  M5.1 done: 0017_state_projection (see "Projection (0017)" above), storage/state_projection.py,
  test_state_projection (Hypothesis), test_migration_0017_roundtrip, docs/phase-e/projection.md
  (claimable 21.4 -> 0.26 ms, DISPATCHING 40.8 -> 0.13 ms).
  M5.2 done: connectors/http_mock (provider wire APIs, harness-only admin/ledger API, servers),
  connectors/http_providers.py (HTTP facades: same surface and exceptions as the in-process mocks,
  so the connectors are unchanged; off-contract answers are never decisive), deploy/stack/ (config,
  providers, worker, loops, processes), run_stack --processes, test_stack_processes (Redis DB 11),
  test_http_providers, test_stack_config; ledger-isolation AST test covers the HTTP path.
  M5.3 done: Stripe adapter offline (see "Stripe sandbox (M5.3)" above): core stripe_profile,
  connectors/stripe_key.py, stripe_payments.py, stripe_fake.py, deploy/sandbox/run_stripe.py and the
  frozen manifest_stripe_v1.json (8 scenarios x 2, <= 150 calls), docs/phase-d/stripe-sandbox-contract.md;
  Stripe key shapes in log redaction and the secret scan; dry run offline in CI (integration).
- STOP 1 reviewed and approved (2026-10-21) with two notes, both applied: the Stripe refund status is
  recorded as non-decisive evidence (provider_facts -> details.provider; structural test: no decision
  reads it) and asynchronous refund failure is documented as out of scope (no webhooks); the macOS
  fork-start risk and the qualification rule (check fork safety first, prefer Linux workers) are in
  docs/phase-e/observations.md. The author creates the Stripe key and says so before M5.8.
  M5.4 done: 0018_provider_limits (see "F16 limits (0018)" above), ConnectorLimits, mock 429/outage
  faults (+ HTTP wire 429/Retry-After), reconciler pacing, test_provider_limits (incl. a 300-operation
  retry storm), test_migration_0018_roundtrip, docs/phase-d/f16-limits.md.
  M5.5 done: tests/integration/test_worker_process_kill.py (Redis DB 10): SIGKILL of a real prefork
  worker (process group) after claim, after the provider applied the effect, before the outcome commit,
  and of a prefork child while the parent lives (WorkerLostError, requeued, refused claim, reconciler
  resolves); one effect and one call each. Harness-only failpoint in deploy/stack/worker.py
  (SAFEACT_STACK_FAILPOINT + _MARKER; refused unless the schema is safeact_it_<hex>; Phase A test).
  M5.6 done: 0019_next_check: safeact_next_check(op) (kind, not_before, deadline, basis; read from
  the same rules: paced lookup, backoff, breaker, lease, approval wait, post-escalation bounds) in
  safeact_api_operation; MCP status carries kind + times only (disclosed in observations); console
  components/NextCheck.tsx; demo/E2E contracts now carry time-scaled limits (deploy/demo/profiles.py
  PACE_LIMITS); tests: test_next_check, API detail, Vitest x2, Playwright next-check.spec.ts.
  M5.7 done: observability/metrics.py (allow-listed labels, never identifiers; per-process exporter
  on SAFEACT_METRICS_PORT; prefork worker in multiprocess mode), 0020_metrics (safeact_metrics():
  durable gauges exported by the reconciler), JSON events dispatch_outcome / lookup / reconciler_move,
  requirements-obs.lock (prometheus-client 0.26.0), docs/phase-e/observability.md.
- M5.8 done: sandbox execution and evidence reviewed: offline and real runs each 16 PASS, 0 DIFFERENT/ERROR; real run 77/150 calls. Evidence: docs/phase-d/stripe-sandbox-v1/REVIEW-M58.md.
- M5.8 follow-up: runner fails on DIFFERENT or ERROR; file handles, Alembic path separator and test-engine xid8 reflection corrected. Three regression tests passed; full regressions reviewed: Python 1053, console 35, typecheck/lint/build passing; targeted warnings absent.
- M5.9 foundation hardened: fresh scenario/arm environments, retained unexpected ledger effects, propagated thread/setup errors, SafeAct safety exit gate, explicit expected terminals, real SIGKILL failpoints and conservative observed coverage; 18 harness regression tests added.
- M5.9 HTTP tracing reviewed: Python 1093 and console checks passed. Four development cases
  had no harness errors or observed safety violations. CONTROL, F01 and F03 completed; F16
  observed 429 then retry but only 1/8 operations completed (7 confirmed failures after retry
  exhaustion). That is a safety/coverage result, not successful completion of the storm.
- M5.9 evidence boundary: stop execution and verify process groups, close provider admission,
  drain active provider calls and consumed held commits before the final ledger snapshot.
  Keep timed views distinct from final views; cleanup effects count for safety but cannot
  inflate useful completion or observed interleaving coverage. Development summary/report
  separates achievable completion, outcomes, errors, N/A and safety; scoring is not frozen.
  Post-change regressions reviewed: Python 1113, console 35, typecheck/lint/build and six
  development evidence cases passed. Both F07 cases completed; consumed held commits drained,
  process groups stopped and no ledger events appeared after the quiescence boundary. F16
  remains 1/8 useful completion with no observed safety violations in that case.
- M5.9 F06: replaced a pre-send sleep with an accepted, held provider request under a separate
  q-v2-inflight mock contract (6 s request deadline, 3 s worker lease; broker visibility 12 s).
  Independent DB samples
  bracket the same persisted lease as live then expired within actual provider/client intervals;
  no host/database wall-clock alignment is assumed. Audit rows retain committed commit_xid
  visibility; that alone does not prove lost acknowledgements. F07 now declares CRM completion
  achievable because this fixture restores indexed lookup inside the observation window.
  Post-change regressions reviewed: Python 1132, console 35 and all console checks passed.
  Seven development cases completed. F06: 4/4 useful completions (3 payments, 1 CRM), one
  effect each; provider intervals of 4.0-5.0 s contained both live and expired DB lease samples.
  Control completed 1/1; F07 completed 2/2. All projection and quiescence checks passed.
- M5.9 execution/shutdown evidence reviewed: Python 1162, console 35 and all console
  checks passed. Raw results, coverage and scoring reproduced for twelve development cases.
  Control completed 1/1; F05 completed 2/2 achievable operations with three expected
  escalations; F11 completed 5/5 with one expected escalation. Each F05 kill has a real -9
  root exit and retained worker generation; each F11 lost ack joins its runtime exception
  and pre-commit xid to an independently committed audit row. All quiescence checks passed.
  The macOS shutdown fix resolves ambiguous EPERM through a checked numeric process table;
  surviving groups, unreadable observations and unreaped owned roots still fail the gate.
- M5.9 queued-broker evidence reviewed: Python 1190, console 35, typecheck/lint/build
  and diff check passed. Raw oracle, observed coverage and scoring independently replayed
  for twelve CONTROL/F04/F08 development cases: all 12 operations completed safely.
  Every scheduled loss observed a real queued message, the consumer's -9 exit and
  disconnected Redis readers, a waitpid-confirmed relay stop, independent RECORDED
  delivery rows, nonempty atomic removal and a fresh task/delivery consumed afterward.
  Four F08 cases also observed a supervised after-claim group kill. All three connector
  contracts were included; no harness errors, drift or incomplete quiescence were reported.
- M5.9 early-redelivery evidence reviewed: Python 1214, console 35, typecheck/lint/build
  and diff check passed. Oracle, coverage and scoring independently replayed for all
  sixteen CONTROL/F04/F08 development cases, each safely completed. All four early
  redeliveries (payments twice, CRM and email) retained the same task/delivery identity
  and ran in two real prefork children. The duplicate returned not_claimable while
  independent DB samples retained the original live DISPATCHING fence. Each case
  made one provider POST, one provider call and one effect. Quiescence was complete.
  This proves forced early transport restoration; normal visibility expiry is not claimed.
- M5.9 isolated Redis kill reviewed: Python 1246, console 35, typecheck/lint/build
  and diff check passed. Oracle, coverage and numerical scoring independently replayed
  for all nineteen CONTROL/F04/F08 cases; all 19 completed with one effect each.
  Three separately owned Redis 7.4.2 containers (payments, CRM, email) each had an
  actual queued message, independent RECORDED rows, Docker signal-9/exit-137 kill/die
  events, non-OOM death, a changed run id and empty DB after restart, then a fresh
  task/delivery consumed. Creation/removal ownership matched and removal followed
  execution/provider quiescence. The shared Redis process run id stayed unchanged.
  Eight queued-flush and four concurrent early-redelivery cases also retained direct
  coverage. No harness errors, integrity drift or incomplete cleanup were reported.
  The private container configuration and authentication constraints are unchanged.
- M5.9 recovery coverage in progress: provider outage begin/end observations share
  the request lock. Continuous outage intervals must contain independent DB samples
  of the same investigation deadline as live then passed, plus a real failed HTTP
  and provider request. Key expiry records the actual removal of a stored payment
  key at its provider-clock retention boundary; a lost response and unresolved DB
  samples before/after removal are required. Sampling never advances a clock or
  changes retention. Late-commit variants release a consumed hold only after an
  independent autocommit connection sees committed escalation; coverage requires
  that DB observation before the first effect, with the original lost HTTP request
  earlier. Revealing an already-committed effect does not count as a late commit.
  Host monotonic query intervals order sources; DB wall clocks are never aligned
  to the host. Cleanup-only events cannot inflate timed fault coverage.
  Preserve the first three F07/F16 schedules and add payments/CRM variants at 3/4.
  The development --cases selector names existing cases explicitly. F09 now declares
  unresolved escalation as its expected terminal after the key expires. Payments
  outage completion stays achievable because a scheduler can conserve protected
  attempts; an arm exhausting attempts is still a useful-completion failure.
  The original F16 storm result (1/8 useful completion) remains unchanged evidence.
  Full Mac regressions passed: Python 1273 (706/536/21/8/2), console 35 and
  typecheck/lint/build/diff checks. CONTROL and five F07 cases completed safely
  (6/6 useful, one effect per operation, no harness errors/drift, full quiescence).
  Both independent post-escalation commit proofs were observed and replayed.
  The F07 evidence gate correctly stopped: payment cases 0000/0002 returned
  in_progress before commit, never absent. The q-v2-eventual profile was not wired
  to the shared payments mock's lookup index; hide_new_effects was ineffective.
- M5.9 recovery fixture verification (2026-10-22): the eventual-payment
  fixture and the observed-empty-HTTP-lookup release barrier passed Phase A
  and real-socket tests. All 16 CONTROL/F07/F09/F16 development cases ran with
  zero harness errors, observed safety violations, projection drift or
  incomplete quiescence. Raw oracle, interleavings, summary, report and
  coverage counts were independently replayed and exactly matched. Evidence:
  docs/phase-e/dev-recovery-20261022/REVIEW.md (raw traces, source/artifact
  hashes, environment and secret scan). Useful completion 14/22 achievable:
  CONTROL 3/3, F07 5/5, F16 6/14; F09 has no achievable automatic completions.
  All three original F07 cases observed an actual empty HTTP lookup before
  commit; both post-escalation commits, all three key expiries and three
  outage/deadline crossings received direct coverage. The original F16
  1/8 storm result remains, and the payment deadline-outage case also fails
  useful completion. Scoring and corpus are not frozen; M5.9 is not complete.
- Resume audit corrected stale README/Stripe status and added the Phase D
  evidence index (docs/phase-d/phase-d-exit-evidence.md). No key contents were
  inspected and no new Stripe/model request was made. A concurrent API E2E
  run exposed a single-stream SSE test assumption; the journey now honors
  bounded reconnects before its first change, with a new Phase A regression.
  Original failure and explanation: docs/phase-e/observations.md. Post-fix
  checks: Phase A 725, isolated 21, API E2E 8, agent harness 2, Vitest 25,
  Playwright 10, typecheck/lint/build passed. DB/broker discovery passed 535
  with two opt-in skips; those two isolated-Redis tests passed separately.
  Final unique total: Python 1293, console 35; all source/artifact hashes,
  changed documentation links, the development secret scan and diff checks passed.
  while that mock kept its default strong lookup. F09/F16 did not run after stop.
  Original uncredited payments traces and the failed packet remain unchanged.
- M5.9 F07 fixture correction in progress: register qualification payment profiles
  per native key. Eventual keys expose the delayed index (absent while pending,
  hidden after commit until reveal); strong keys still expose in_progress/found.
  Dispatch retains its existing in-flight exclusion, parameter binding, replay
  and retention guards. All future baseline arms must register their actual
  payment keys with the same fixture API and contract, including changed keys.
  The original seeded F07 delays are minimum holds after acceptance; release now
  also waits for a uniquely joined empty provider lookup and completed HTTP 200
  GET on the same key/provider. An in_progress answer, missing response, overlapping
  request or later reveal cannot substitute. Coverage requires the empty HTTP
  response before the FIRST effect. Cancellation leaves holds to ordered cleanup.
  F06 holds, strong payment variants and post-escalation variants are unchanged.
  Added 14 pure regression cases and one real HTTP integration case. Local
  preparation passed 226 focused tests, replayed all 60 prior saved oracle/coverage/
  numerical rows unchanged, and passed real loopback HTTP probes for payments/CRM:
  socket timeout, empty GET received, gated release, one effect, reveal then found.
  The recovery development run completed 16 CONTROL/F07/F09/F16 scenarios
  (29 operations): 0 harness errors, safety failures, duplicates, orphans,
  unaccounted operations, false claims, or projection drift; all quiescence gates
  passed. F07 completed 5/5 with one effect each, observing four empty-lookup
  late commits and both post-escalation commits. F09 escalated all 3/3 operations
  as expected after key expiry; each had one effect and two same-key retries
  before expiry, with no dispatch after the retention boundary. F16
  retained the original 1/8 storm completion; the payments long outage completed
  4/5 achievable operations, while the deadline-crossing payment case missed its
  achievable completion and escalated. CRM outage cases remained unresolved as
  expected. Coverage observed three retention expiries, one 429-then-retry, and
  three outages spanning a deadline. Overall useful completion was 13/22
  achievable operations. Raw development evidence:
  `/tmp/safeact-m59-recovery-20261022-1541`.
  Recovery scoring replayed the same 16 raw rows with new per-operation
  verification delay and provider-call metrics: 243 calls (63 dispatch,
  180 lookup), $0.00 external provider cost (mock-only), verification-delay
  p50 9.98 s / p95 12.76 s across 14 measurable operations. The replay is at
  `/tmp/safeact-m59-metrics-replay-20261022`; its safety and usefulness totals
  match the original run. Verification delay is measured from the first
  ambiguous dispatch error to the first completed subsequent lookup; it does
  not represent Stripe or other real-provider pricing.
  Before the scoring-metric addition, full Mac regressions passed: Python 1288
  (Phase A 720, PostgreSQL + broker 537, isolated PostgreSQL 21, API E2E 8,
  agent harness 2), console 35, and typecheck/lint/build. After the metric
  addition, the complete Phase A suite passed 724 tests. The two isolated-Redis
  tests passed under their explicit opt-in; `.github/workflows/ci.yml` now
  enables that opt-in in the integration job.
  The earlier F07 fixture gate failure and its original traces remain unchanged.
- M5.9 F13 implemented (2026-10-22, uncommitted): actual fixed charge/order
  workflow admission, separate child observations and refund-only faults;
  independent committed DB bindings and provider payload/effect evidence;
  forward/inverse counts, inverse-duplicate and false-restoration safety gates.
  Five explicit F13 modes: lost refund reply verified by lookup, persistent
  reply loss with hidden lookup, rejected refund, uncertain order with no
  refund, and a normal completed workflow. Observer failures fail the harness;
  ambiguous or unfinished HTTP joins and cleanup-only effects earn no coverage.
  Final development run: 8 cases, 0 errors/safety violations/drift, full
  quiescence; 5/5 achievable units (2 workflows + 3 controls), 10 forward and
  2 inverse effects. Exact raw oracle/coverage/scoring/report replay passed.
  Evidence: docs/phase-e/dev-workflows-20261022/REVIEW.md. Design and baseline
  adapter limits: docs/phase-e/workflow-qualification.md. Added 19 Phase A
  counterexamples and 1 real PostgreSQL observer test (uncommitted claim
  invisible until commit). Phase A 744, isolated 21, API E2E 8, agent 2,
  Vitest 25, Playwright 10, typecheck/lint/build passed. Final DB/broker suite:
  538 passed, no skips; total Python 1313 and console 35. Original full run:
  537 passed, one concurrent-login count failed after a statement timeout.
  Unchanged targeted and full reruns passed; all outcomes are retained in the
  evidence's regressions/ directory. A verified source archive preserves this
  uncommitted version. Source/artifact hashes, raw replay, documentation links
  and runtime/source secret scans passed. No commit, Stripe/model call or
  frozen-corpus run was performed. The next driver plan is in
  docs/phase-e/review-agent-qualification-plan.md, including actual credential
  rotation and the mixed-provider held-out combination construction audit.
- Remaining M5.9: baseline workflow
  evidence adapters; audit achievable
  declarations before freeze, including F16's global rejection budget; retain
  its original 1/8 evidence. Phase D exit rollup: docs/phase-d/phase-d-exit-evidence.md.
- Customer-support improvements (2026-10-22, uncommitted): console workflow
  creation, retained per-principal/tenant pending intent across reloads, derived
  status and separate charge/order/refund cards, evidence timeline and next-safe
  action explanations; workflow steps link back to the whole workflow. Demo CRM
  case 2004 is a permanent no-effect rejection and exercises a separate refund.
  examples/replacement.py uses the existing local authenticated API and retains
  caller-selected business identity; examples/outcomes.py summarizes saved
  results without executing scenarios. README and docs/demo/ explain the use
  case, walkthrough, provenance and measured outcomes, retaining F16 failures.
  The new real API check exposed replay with a changed fee being accepted as
  identical. New migration 0021 binds the original charge as well as the plan;
  the internal SQLAlchemy workflow path now makes the same check. No old frozen
  SQL changed. Design: docs/phase-e/workflow-replay-binding.md. Applied 0021 to
  the local development DB and reran local admission-role provisioning. Existing
  demo environments require a separate bootstrap/restart for the new migration
  and grant; do not reset someone's active demo without authorization. Historical
  evidence packages retain their archived source and have not been rewritten.
  Follow-up 0022 covers a second unique-index race during duplicate admission:
  ON CONFLICT handles both unique indexes, then still requires the existing
  immutable identity/requester comparison. Internal record_intent uses the same
  insert handling. An API E2E test's hidden worker-thread exception exposed the
  HTTP 500; futures now propagate every failure and assert all four replies.
  Barrier-synchronized API/internal admission tests verify one identity and one
  complete audit/outbox bundle, plus changed-payload conflicts. Applied 0022
  locally and reprovisioned the existing local admission role. No old frozen
  migration SQL, constraints, states or permissions were changed.
  Final UI review also replaced positional grant selections with stable exact
  grant identities in both submission forms, and refuses the single-action
  form's empty resource placeholder. Tests cover grants inserted before the
  selected customer/case/charge, preventing silent target changes on refresh.
- F14/F15 development drivers (2026-10-23, uncommitted): actual human API and
  official MCP clients against separate processes, exact generated q-v2 grants,
  same-principal credential rotation and attack snapshots. Nine CONTROL/F14/F15
  cases passed with 9/9 achievable completions, nine effects, 55 provider calls,
  zero harness errors, observed safety violations, drift or incomplete quiescence.
  All six new critical boundary interleavings were directly observed. Raw oracle,
  coverage, summary and report independently replayed exactly. F14's denominator
  includes scripted authorized human reviewers; F15 is not a paid model evaluation.
  Design: docs/phase-e/review-agent-qualification-plan.md. Evidence is retained
  in docs/phase-e/dev-boundaries-20261022/. Initial harness/fixture failures are
  retained, including an indirect PostgreSQL lock-wait chain and permanent index
  loss mistakenly scheduled as revealable. No SUT transition/auth SQL changed.
  Final Phase A 795, integration 547 plus nine baseline admission tests, isolated
  security 21, API E2E 9, agent harness 2 and console 51 passed, as did typecheck,
  lint and build. Original full database discovery had one existing outage
  polling-count failure (10 vs >30); unchanged isolated and full reruns passed.
  Both full outcomes are retained, with no relaxed assertion or timeout.
- M5.10 foundation (2026-10-23, uncommitted): docs/phase-e/b3-design.md written
  before implementation from blueprint section 04 and primary AWS/Stripe sources.
  chaos/baselines/recovery.py implements independent B3 recovery decisions and
  bounded jitter against the shared contracts, without SafeAct SQL or recovery
  workers. Twenty focused offline tests pass, including an actual late CRM commit after
  an empty lookup. Initial test expectations incorrectly exceeded the email/CRM
  attempt budgets; fixtures now respect the actual q-v2 budgets. store.py now
  commits intent/audit/outbox in one ordinary PostgreSQL transaction; nine own
  integration tests prove exact replay/conflict, six concurrent callers, lost
  acknowledgement, rollback on publication failure and independent atomic
  visibility. No baseline arm is registered or claimed qualified.
- M5.10 execution slice (2026-10-23, uncommitted): own ordinary PostgreSQL
  worker/relay/reconciler, committed dispatch fencing, leased token/version
  claims, late-response retention, scoped negative lookup evidence, bounded
  scheduling, token buckets/breaker/backoff, private process configuration and
  Celery tasks. Thirty actual DB/HTTP/process tests pass, including six duplicate
  deliveries, real SIGKILL after provider application, lost commit acknowledgements,
  in-flight lease expiry and actual Redis message loss followed by durable
  republishing. Application roles cannot delete attempts, observations or audit.
  The due-work query prevents 101 retired scheduling rows from starving new work.
  Normalized evidence adapters, registration and B0-B2
  remain required; no baseline comparison result is claimed.
  Building B3 reproduced a real SafeAct negative-evidence bug when an HTTP lookup
  crossed actual native-key expiry. A shared connector completion guard now checks
  fresh DB time before proof is recorded. The original failing real HTTP/DB test
  and its passing rerun are retained; seven offline boundary checks also pass.
  Full Phase A additionally found Celery global task contamination; both factories
  now register private tasks, with both construction orders tested. Original
  failed test runs and the final sandbox socket refusal are retained separately.
  No frozen SQL, lifecycle pairs, migration or authorization grant changed in
  this execution slice. Plans: docs/phase-e/b3-execution-plan.md and
  docs/phase-e/lookup-completion-binding.md. Full verification: 1430 Python,
  51 console, typecheck/lint/build. No real Stripe/model calls or commits.
- M5.10 workflow/review slice (2026-10-23, uncommitted): B3's own fixed workflow
  commits request/charge/binding/audit/outbox atomically, compares the whole
  original request on replay and serializes advancement through its original
  charge job. Confirmed charge admits the order; proved order failure admits
  a separate refund of the actual charge/amount/account. Uncertain or rejected
  compensation keeps the forward history and unresolved status. Six concurrent
  creators/advancers, real HTTP forward/inverse faults, lost commit acknowledgements,
  rollback, corrupt inverse bindings and a real advancer process are tested.
  Its local human review API uses existing pgcrypto bcrypt-12, hash-only bearer
  sessions, live tenant/roles and reviewer independence. Version/evidence/policy-
  bound decisions serialize under ordinary row locks; the winning key replays,
  stale proposals refuse and every approval is single-use at a retry fence or
  lookup reservation. Fresh safety/evidence/expiry checks remain required.
  Late lookup evidence can now resolve ready work without broker delivery.
  Eighteen own workflow and nineteen own review integration tests plus six
  input-boundary tests were added; the full discovery includes them all.
  Twelve retained development cases passed with 18 effects (15 forward, three
  inverses), 27 dispatches, 20 lookups and complete cleanup. Read-only replay
  matched; fifteen synthetic verifier counterexamples were rejected separately.
  Original privilege/join/dependency/fixture failures and the Uvicorn SIGTERM
  verifier assumption remain diagnostic evidence. CRM 429 ambiguity still
  refuses retry authority; payments' actual rejection preserves Retry-After.
  Evidence: docs/phase-e/dev-baseline-workflow-review-20261023/REVIEW.md.
  Source/archive/secret checks cover 410 runtime/test files; earlier 136 retained
  artifacts remain unchanged. No product frozen SQL, migration or grant changed;
  no commit, real provider/model call or final qualification was performed.
- M5.10 database observer slice (2026-10-23, uncommitted): independent read-only
  repeatable-read B3 snapshots expose committed operations/audit, actual dispatch
  leases, recovery deadlines and native workflow request/child bindings. The
  observer never calls runtime/workflow locking methods, SafeAct SQL or the
  provider ledger. Expiry predicates use the database clock. Common workflow
  scoring validates original tenant/requester/child identity, payload, contracts,
  accounts and the inverse's exact actual-charge resource, without synthesizing
  a stored SafeAct plan. Twelve new real PostgreSQL/HTTP tests and eight new
  offline oracle tests pass. Five retained workflow cases pass common-oracle
  safety checks with six forward/three inverse effects and complete cleanup;
  hidden/rejected inverses remain unresolved. Seventeen historical SafeAct
  results replay unchanged; 27 synthetic mutations reject separately. Database-
  only records receive no shared critical interleaving credit. Full regressions
  pass: 1493 Python, 51 console, typecheck/lint/build. Source verification covers
  413 files; 211 artifacts in six earlier packets remain unchanged. Plan:
  docs/phase-e/b3-observer-plan.md. Evidence:
  docs/phase-e/dev-baseline-observer-20261023/REVIEW.md. No partial comparison arm
  is registered, and no final qualification, commit or real provider/model call
  is authorized by this slice.
- M5.10 process observation slice (2026-10-23, uncommitted): B3-owned strict
  HTTP/execution/broker traces preserve actual wire exchanges, commit ids,
  exception classes and Celery app/task/queue/delivery/process identities.
  Clients are observed after fork; app close disconnects signal observers.
  Private regular single-link files, schema/field/identity validation, append
  locks and complete/duplicate-record checks protect the observation boundary.
  Native LostAcknowledgement coverage still requires an independently committed
  audit xid and matching stage/process/order. Seven real process checks pass
  with nine dispatches, two lookups, nine effects and complete cleanup; 28
  synthetic saved-data mutations reject. Earlier five B3 adapter records and
  seventeen SafeAct results replay unchanged; all 251 earlier packet artifacts
  remain unchanged. A minimal-config factory access and intermittent concurrent
  file creation error were found/fixed; original failures are retained, and
  fifty unchanged concurrent repetitions pass. Full regressions pass: 1513
  Python, 51 console, typecheck/lint/build. The source snapshot covers 416
  files, with four unchanged base-only references. Plan:
  docs/phase-e/b3-process-observation-plan.md. Evidence:
  docs/phase-e/dev-baseline-process-trace-20261023/REVIEW.md. No comparison arm,
  commit, real provider/model call or final qualification is part of this slice.
- M5.10 owned-stack slice (2026-10-23, uncommitted): B3 owns a disposable
  database, private configuration/queue, all process generations and an
  independent mock registration controller. The relay waits outside its
  transaction after reservation commit and before publication for exact
  schema/PID/xid/fresh-generation confirmation; workflow children and replays
  use the same gate. No worker/advancer gets mock administration or ledger
  access. Checked quiescence precedes provider admission closure, call/held
  commit drain, final observation and schema/configuration removal. An
  unrequested clean exit remains failure after replacement. Selected actual
  process checks pass: 13 cases, four explicit negative integrity checks, 11
  dispatches, ten effects and complete cleanup across 34 process generations.
  Offline replay passes and rejects 33 saved-data mutations; earlier seven
  process cases, five B3 oracle cases and seventeen SafeAct rows remain unchanged.
  Full regressions pass: 1537 Python, 51 console, typecheck/lint/build. The
  source snapshot covers 420 files; 300 artifacts in eight earlier packets
  remain unchanged. Gate overhead is retained separately, with no cross-arm
  timing claim. Original fixture/replay diagnostic failures remain in the packet.
  Plan: docs/phase-e/b3-owned-stack-plan.md. Evidence:
  docs/phase-e/dev-baseline-owned-stack-20261023/REVIEW.md. No partial arm,
  real provider/model calls, commit or final qualification is part of this slice.
- Earlier checkpoint (before pooling): STOP 2 — PRE-RUN REVIEW. Implementation, full regression
  and the pre-run evidence packet are complete. B0–B3 are registered; native broker/fault/F14
  adapters and construction/scoring audits are implemented. The 1,200-scenario
  candidate, 80-case smoke sample and backend source hashes are pinned. Do not
  execute frozen smoke/calibration/full scopes before the author's explicit go.
  Post-review calibration writes a verified slot amendment; full execution
  refuses an arbitrary slot count and permits one primary invocation per freeze.
- Earlier pre-STOP-2 continuation (2026-10-23, historical): the B3 common-runner
  adapter now supports native fault files, actual worker deaths, lease history,
  queued loss, early restoration and owned Redis death. Native authenticated
  proposal display and F14 lock/decision/evidence/permission-consumption joins
  are implemented. All 75 existing development cases ran: 72 applicable rows
  with zero harness/safety errors, all 24 applicable critical interleavings;
  three genuine F15 N/A rows. Current oracle/coverage replay is unchanged.
  B3 is now published for development; frozen qualification remains denied.
  B0-B2 implementation, capability/applicability review, full regressions and
  source/evidence sealing remain in progress. Plans:
  docs/phase-e/pre-stop2-implementation-plan.md and
  docs/phase-e/weaker-baseline-implementation-plan.md. First broker/fixture/
  native-review state mismatches and corrected diagnostics remain in /tmp
  pending the next evidence packet. Last full regression baseline stays 1537
  Python / 51 console until final-source discovery completes.
- Completed pre-STOP-2 continuation (2026-10-23, uncommitted): all comparison
  adapters and source/contract construction audits are complete. Saved-data
  replay validates 375 selected development rows from 670 retained rows across
  eight runs. SafeAct has all 28 declared labels and B3 all 25 applicable labels,
  including claim/fence/outcome acknowledgement loss and concurrent refusal.
  Weaker-arm missing labels remain explicit, with genuine N/A declared before
  outcomes. Twenty-five new saved-data mutations are rejected; the earlier
  33-mutation check and historical replay pass unchanged. All 364 artifacts in
  nine historical packets verify. Full regression is 1626 Python / 51 console,
  plus typecheck/lint/build. The final CI-only amendment parses but has not been
  dispatched; exact manifest reconstruction proves Python SUT bytes did not
  change after integration began. The packet snapshots 470 source files plus
  four exact base-only references. No frozen smoke/calibration/full outcome,
  real Stripe/model call, staging, commit or push occurred. Review:
  docs/phase-e/dev-baseline-adapter-20261023/REVIEW.md. Pause for the author here;
  announce FINAL BIG QUALIFICATION RUN and obtain explicit go before execution.
- Open observations: runtime error logs require attribution; SQL canonical-JSON enforcement and runtime credential/subject binding remain stated limitations.
- Deferred, NOT reduced: (projection done in M5.1). Done in M3: workflow-creation authorization binding (0012). JWT-to-session binding:
  closed for the human API path (0011); worker side re-scoped, still open.

## v1 milestones (in order)
1. Phase B vertical slice: one mock refund through admission → claim → dispatch-risk
   fence → fake provider → outcome persistence → crash recovery.
2. Three connector profiles + reconciliation (timeout-after-success, late commit,
   unavailable evidence, non-idempotent email).
3. Authenticated FastAPI + React console (submit/track, investigate, approve/reject)
   with version-bound human approval.
4. MCP server + bounded agent; compare guarded agent vs direct-tool agent vs
   deterministic caller.
5. Evaluation vs baselines, Docker Compose, CI, README with results table, demo video.

Later (full original scope): real payments sandbox, all 16 fault families (F01–F16),
1,000+ seeded scenarios vs baselines B0–B3, full observability.

## How to work in this repo
- Run the full test suite before and after every change. Never finish with failing tests.
- Plan first for anything touching state transitions, locking, or authorization.
- Prefer completing a whole milestone slice over many tiny isolated steps.
- Explain design decisions briefly in commit messages so the author can defend them
  in interviews.
- After each step, update the "Current status" section above.

## Full specification
The full blueprint is docs/Project_1_SafeAct_Updated.pdf. Read the relevant section before planning any milestone.
