# SafeAct — Project Instructions for Claude Code

> **Note (2026-10-27): private development history.** Commit hashes, PR numbers, CI run
> ids, branch names and local paths (`~/…`, `/Users/…`) in this file and in the docs/phase-e
> and docs/phase-d records refer to the private development history and the author's machine;
> the public repository starts from a later snapshot. Results stay verifiable through the
> SHA-256 pins (`chaos/source_qual_v1.json`, the manifests, `artifact-sha256.json`);
> re-running a v1 diagnostic from the qualified commit needs that private history.
> Phase A's freeze test (`tests/phase_a/test_qualification_freeze.py`) checks that every file
> in the pinned source inventory (`chaos/source_qual_v1.json`) still matches its qualified
> SHA-256, except the changes, additions and removals declared in
> `docs/phase-e/qual-v1/post-qualification-changes.json`. The declared list must be exact, no
> declared change may touch the lifecycle SQL, migrations, `core/lifecycle.py`,
> `core/provider_keys.py` or `storage/durable_lifecycle_sql.py`, and the frozen manifest, its
> inputs and the scenarios must be unchanged.

## What this project is
SafeAct is an execution-safety layer for AI-agent actions. It prevents an agent from
executing an irreversible action twice, acting without authorization, or making an
unsafe recovery decision after a crash. Goal: a portfolio-grade system that proves safe
external-action execution end to end, not a collection of isolated backend tests.
Full blueprint: docs/Project_1_SafeAct_Updated.pdf (read the relevant section before
planning any milestone). History: docs/phase-e/status-log.md.

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

## Lifecycle and database rules
RECORDED → CLAIMED → DISPATCHING → SUCCEEDED | FAILED_CONFIRMED | UNKNOWN
UNKNOWN → VERIFYING → SUCCEEDED | FAILED_CONFIRMED | RETRY_ELIGIBLE | ESCALATED_UNRESOLVED
Enforce legal transitions, worker ownership, version checks and durable audit evidence.
Source of truth: core/lifecycle.py; each frozen SQL module seeds its pairs and a test proves
their union equals core. storage/schema.py does not describe 0003+ objects (the Alembic
include_object guard keeps autogenerate from dropping them).
- **Frozen:** a migration's rendered SQL (storage/lifecycle_sql/vNNNN.py) never changes once
  committed; golden digests in tests/phase_a/test_migration_sql_frozen.py. Only allowed edit:
  adding an exact schema name to the allowlist (done once: "safeact_demo" in v0003).
- **Evidence gates:** FAILED_CONFIRMED needs authoritative non-application for every attempt and
  no applied evidence (SAT09); SUCCEEDED needs applied-effect evidence. Empty, NOT_FOUND or
  INCONCLUSIVE never reaches FAILED_CONFIRMED. VERIFYING → FAILED_CONFIRMED is a deliberate
  deviation from blueprint §05 (needed for §09 and F02).
- **Deviations:** ESCALATED_UNRESOLVED → VERIFYING reopen only on new decisive evidence after the
  escalation, within max_reopens (0005). CLAIMED → RETRY_ELIGIBLE / UNKNOWN: nothing is dispatched
  while CLAIMED (0006). RETRY_ELIGIBLE → ESCALATED_UNRESOLVED only when safeact_reclaim_blocked()
  says so (0008). RECORDED → CANCELLED_BEFORE_CLAIM only via safeact_api_cancel, terminal, never
  for workflow steps; later cancels are recorded as refused (0012).
- **Retry (0007):** safeact_retry_basis = proof of non-application for every earlier attempt, or a
  native key before first fence + retention - margin; checked at VERIFYING → RETRY_ELIGIBLE and at
  the fence (SAT12); attempt budget and max 3 abandoned claims (SAT13). DISPATCHING → UNKNOWN
  reasons are a fixed vocabulary (core.lifecycle.AMBIGUOUS_REASONS / RETRY_PENDING_REASON).
- **Provider keys (0008):** derived server-side from (tenant, request key, connector, account) in
  core/provider_keys.py; never caller-chosen; a CHECK recomputes it; unique on
  (provider_account_id, provider_idempotency_key). Admission inserts are untargeted ON CONFLICT
  DO NOTHING (0022: identical concurrent admissions may meet either index first); a replay must
  match every identity field of the request-key row, any other conflict is refused with the
  SQLSTATE and constraint PostgreSQL names (storage/submission.py).
- **Identity (0011):** bcrypt-12, hash-only sessions (8 h absolute, 30 min idle), verified in SQL;
  login never raises on bad credentials; lockouts 5/15 min per account, 20 per source. The API role
  may EXECUTE only safeact_api_* and never logs statements/parameters. SAT17 unauthenticated,
  SAT18 CSRF, SAT19 role, SAT20 identity invalid. Internal helpers are SECURITY INVOKER.
- **Admission (0012, 0021, 0022):** tenant/actor from the session, exact action grants (SAT21);
  same key = same operation, changed request = SAT22, invalid = SAT23. Reads tenant-scoped,
  otherwise 404. The API picks the connector and canonicalizes payloads (api/catalog.py).
  Workflow replay is bound to the original charge as well as the plan (0021).
- **Review (0013, 0015):** investigators propose REVERIFY / PROTECTED_RETRY / ATTEST_APPLIED; a
  binding digest covers seq, state, payload hash, evidence digest, contract, action, expiry and the
  review policy row. A human reviewer who is neither proposer nor requester decides once
  (SAT24/SAT25); stale bindings are a recorded 409 result. Reviewed contracts need approval for
  retries (SAT27); retry_approval_expired → FAILED_CONFIRMED only with system proof for every
  attempt. Human provenance is allowed only for applied effects.
- **Change feed (0014):** commit_xid (xid8) + safeact_api_changes; late commits are delayed, never
  skipped; GET /events SSE with Last-Event-ID catch-up. idle_in_transaction_session_timeout=60s.
- **Agent keys (0016):** 'sak_'+64 hex, SHA-256 only, scopes ⊆ {submit, investigate}, ≤ 30 days.
  The MCP role may EXECUTE only safeact_mcp_*. Agents never pass request keys, get SAT28 for a second
  live operation on one target (known limit), and may propose only REVERIFY / PROTECTED_RETRY.
- **Projection (0017):** safeact_state_projection written only in the protected path, same
  transaction; any write that is not the next log row is refused (SAT29). projection_drift is
  checked before every disposable schema is dropped.
- **F16 limits (0018):** opt-in per contract; backoff not_before (SAT30), per-account breaker and
  token bucket at claim (SAT31), paced lookups. Details: docs/phase-d/f16-limits.md.
- **Next check / metrics (0019, 0020):** safeact_next_check and safeact_metrics(); metric labels are
  allow-listed and never identifiers.
- **Stripe sandbox:** connector stripe-test, raw HTTPS via httpx2, Stripe-Version 2026-09-30.endive.
  Key: restricted rk_test_ only, ~/.config/safeact/stripe.key (mode 600, outside the repo). Claude
  never reads, prints or logs it. Real calls need SAFEACT_ALLOW_STRIPE_SANDBOX=1, never in CI;
  any livemode object stops the run. Manifest deploy/sandbox/manifest_stripe_v1.json is frozen.
- **OpenAI (M4):** key ~/.config/safeact/openai.key, never read by Claude; $4 total cap
  (spend.jsonl); paid runs only with the author's go and a frozen manifest.

## Connector profiles
- Payments: native idempotency, evidence lookup, retention window, refund scenario.
- CRM: no guaranteed idempotency; delayed/incomplete lookup may leave it unresolved.
- Email: irreversible, no reliable dedup; ambiguous dispatch must never be blindly repeated.

## Commands
Run from the repo root with `.venv/bin/python` (not conda base). Tests use unittest, not pytest.
- Dev PostgreSQL + Redis (`.env` chmod 600): `docker compose --env-file .env -f deploy/compose.yaml up -d`
  (Toxiproxy for qualification: add `--profile chaos`).
- Phase A: `.venv/bin/python -m unittest discover -s tests/phase_a -t . -p 'test_*.py'`
- PostgreSQL + broker (~22 min; Redis test DBs 9–15; isolated Redis tests own their containers):
  `SAFEACT_RUN_DB_INTEGRATION=1 SAFEACT_RUN_DISPOSABLE_DB_TESTS=1 SAFEACT_RUN_DB_SECURITY_TESTS=1 SAFEACT_RUN_BROKER_TESTS=1 SAFEACT_RUN_ISOLATED_REDIS_TESTS=1 .venv/bin/python -m unittest discover -s tests/integration -t . -p 'test_*.py'`
- Isolated security (PostgreSQL 14.23 `initdb`/`pg_ctl`/`postgres` on PATH, non-root):
  `SAFEACT_RUN_ISOLATED_CLUSTER_TESTS=1 .venv/bin/python -m unittest discover -s tests/isolated -t . -p 'test_*.py'`
- API E2E (Redis DB 14): `SAFEACT_RUN_API_E2E=1 .venv/bin/python -m unittest discover -s tests/e2e_api -t . -p 'test_*.py'`
- Agent harness (Redis DB 12): `SAFEACT_RUN_AGENT_HARNESS=1 .venv/bin/python -m unittest discover -s tests/e2e_agent -t . -p 'test_*.py'`
- Console (ui/, Node 24): `cd ui && npm ci`, then `npm test`, `npm run typecheck`, `npm run lint`,
  `npm run build`, `npm run dev` (Vite :5173 → API :8000). Browser journeys (Redis DB 13, :8001):
  `npx playwright test`. After an API change: `.venv/bin/python -m deploy.demo.export_openapi`
  then `npm run gen:api` (CI runs `export_openapi --check` and `npm run check:api`).
- Demo (DEMO ONLY): rebuild `.venv/bin/python -m deploy.demo.bootstrap`; run
  `.venv/bin/python -m deploy.demo.run_stack` (API :8000, MCP 127.0.0.1:8002/mcp; agents use the
  demo-*-agent keys in .demo-credentials) or `--processes` (separate OS processes, mocks on
  8101-8103, harness-only admin on 8109, logs in .stack-logs/; macOS worker forces fork).
- Compose demo stack (DEMO ONLY; `.env` mode 600 from `.env.example`; ports via SAFEACT_CONSOLE_PORT /
  SAFEACT_MCP_PORT in the shell, never in `.env`): `docker compose --env-file .env -f deploy/compose.stack.yaml
  up --build --wait` (console :8080, MCP 127.0.0.1:8002; credentials: `exec api cat /state/demo-credentials`).
  Fresh-clone check (A12): `deploy/fresh_clone_check.sh` (`SAFEACT_FRESH_NO_CACHE=1` for a cold build).
- Demo walkthrough seed (running demo stack; agent beat once per demo, SAT28):
  `.venv/bin/python -m deploy.demo.walkthrough [--show-stale]` (Compose: `--api http://localhost:8080
  --credentials FILE`); script in docs/demo-walkthrough.md.
- Optional Stripe test-mode demo (author's go for real calls): `SAFEACT_ALLOW_STRIPE_SANDBOX=1` then
  `deploy.demo.stripe_demo setup`, `deploy.demo.bootstrap --stripe`, `deploy.demo.run_stack --stripe`,
  `deploy.demo.stripe_demo check`. One real run done 2026-10-25 (docs/phase-e/stripe-demo-v1, PASS);
  fixtures and the call log (.demo-stripe.json, .demo-stripe-calls.jsonl) are gitignored.
- Stripe runner offline: `.venv/bin/python -m deploy.sandbox.run_stripe --fake --out /tmp/stripe`
  (real run only after the author's go).
- Agent eval, scripted and free: `.venv/bin/python -m deploy.evaluation.agent_v1.run --split dev --model scripted-honest --out /tmp/eval`
- Secret scan of evidence: `.venv/bin/python -m deploy.evaluation.agent_v1.secret_scan <dir>`
- Load test (M5.13; dev stack with `--profile chaos`, idle machine, Redis DB 0 must be empty, ~11 min):
  `.venv/bin/python -m chaos.load --out DIR`. The dev PostgreSQL preloads pg_stat_statements; the run
  creates the extension in its own schema and drops it.
- Development evaluator: `.venv/bin/python -m deploy.evaluation.run_corpus_v1`; lifecycle scan
  timings: `deploy.evaluation.measure_lifecycle_scans` (DB integration opt-ins).
- Locks: requirements-{db,broker,api,mcp,agent,obs,test}.lock (pinned; CI installs them).
- Qualification inputs: verify with `tests/phase_a/test_qualification_freeze.py`; rebuild a
  development candidate before STOP 2 only: `.venv/bin/python -m chaos.freeze_inputs --replace-unreviewed`.
  Running `chaos.qualify` (smoke/calibration/full) requires the author's explicit go.
- After each migration: `alembic upgrade head`, then `psql -1` deploy/security/provision_local_admission_role.sql
  (it pins the head revision), then rebuild the demo.
- CI: .github/workflows/ci.yml (push to main, PRs, manual); qualification.yml is manual only and
  gated by `review_approved`. Actions pinned to SHAs; `gh run watch` to follow.

## Author instructions in force
- 2026-10-24: commit each sub-step as the plan says unless the author says otherwise
  (supersedes the 2026-10-22 "do not commit" instruction). Push only when asked.
- Do not run pre-run, frozen smoke, calibration or full qualification until the author says so.
  Before the final run, announce **FINAL BIG QUALIFICATION RUN** prominently and wait for an
  explicit go; general instructions to resume work never authorize it.
- Large raw development evidence (run packets, tarballs, logs, ledgers) is not committed. It lives
  in ~/safeact-evidence/ and is listed with digests in docs/phase-e/evidence-index.md. Sandbox raw
  results are committed with their run. Full qualification raw (2026-10-25): not in git; commit the
  report, summaries, every failure row, coverage, environment, traces of failing/unstable rows and
  digests of raw + compressed copies; compressed raw in ~/safeact-evidence, to be published as a
  GitHub Release asset linked from the README.

## Current status
- Branch: main (all milestone and polish branches merged). Dev DB at 0022_admission_race,
  no execution history (a pre-0004 backup is kept in the author's private notes).
- Milestones 1–4 complete (evidence: docs/phase-c/phase-c-exit-evidence.md,
  docs/phase-d/milestone-3-exit-evidence.md, docs/phase-d/milestone-4-exit-evidence.md; Phase D
  rollup docs/phase-d/phase-d-exit-evidence.md).
- Milestone 5 (plan and decisions in the author's private notes): M5.1–M5.8 done (projection, HTTP stack, Stripe offline
  and real sandbox 16/16 PASS, F16 limits, worker kill, next check, metrics). M5.9 fault drivers and
  M5.10 baselines B0–B3 done in development; connection pooling integrated (author, 2026-10-24).
  M5.11: 1,200-scenario candidate and 80-case smoke sample pinned in chaos/manifest_qual_v1.json,
  STOP 2 closed; calibration and the primary full run v1 executed (2026-10-25).
- STOP 2 (post-pooling) packet: docs/phase-e/stop2-post-pooling-20261024/REVIEW.md (addenda 1-3).
  Author approved B3 in principle and four changes (30d7bd8, 5a23e89, a414398, a485860), then the
  snapshot fix: score the stable snapshot, record/count later differences (c55dbae, manifest
  463a5cc6…). Full dev rerun: docs/phase-e/dev-rerun-snapshot-20261024/REVIEW.md. Awaiting the
  author: SafeAct D-F16-0002 escalated again (all 3 attempts inside the outage, budget spent);
  author chose (a): retained result, stated in report + README (addendum 4, manifest b779fa72…).
- Slot calibration done (2026-10-25): 4 slots (docs/phase-e/qual-v1/calibration/REVIEW.md).
- FINAL BIG QUALIFICATION RUN done (2026-10-25): docs/phase-e/qual-v1/full/REVIEW.md. 6,000/6,000
  rows, exit 0; SafeAct 0 safety failures, 924/937 useful; B3 0 failures, 841/874. The primary
  invocation is spent; any further run is a diagnostic (author's go) or a disclosed new freeze.
  Load-suspect diagnostic done (1 slot, beside the headline): all 169 flagged baseline failures
  recur, 590/590 pairs unchanged (docs/phase-e/qual-v1/diagnostics/REVIEW.md).
  Next (author's decision): publish raw.jsonl.gz as a Release asset on the fresh public repository
  once it exists, then load tests, Compose packaging, demo.
- M5.13–M5.15 restarted 2026-10-25 (the interrupted session's files were kept locally for
  reference only, in the author's private notes). Later changes to pinned source are allowed only when
  declared in docs/phase-e/qual-v1/post-qualification-changes.json (Phase A checks the list is exact;
  frozen runs from this tree are refused).
- M5.15 done (2026-10-25): README rewrite, docs/demo-walkthrough.md + deploy/demo/walkthrough.py,
  optional Stripe test-mode demo profile (one real run 2026-10-25, PASS; author confirmed one
  refund on the dashboard), docs/evidence-pack.md, docs/phase-e/milestone-5-exit-evidence.md.
  CI passed on PR #7 (run 40252297269, commit 26e9761) after test-only fixes; merged.
- M5.14 done: Compose stack + fresh-clone check PASS warm and cold (docs/phase-e/compose-v1/REVIEW.md);
  fresh-clone.yml runs on push to main / manual only; it has passed on every merge to main since.
- M5.13 load test done: docs/phase-e/load-v1/REVIEW.md. 14,710 operations, 0 duplicates, 0 drift;
  execution saturates at ~40-48 terminal/s (4 workers, 20 ms mocks), the API admits up to 120/s; relay
  republishing under backlog recorded in observations.md.
- Tests (2026-10-25, M5.15 tree): Python 1714 (Phase A 962, PostgreSQL + broker 715, isolated 21,
  API E2E 14, agent harness 2), console 52 (Vitest 38, Playwright 14), typecheck/lint/build clean.
- Console polish (2026-10-26, branch feature/console-polish, UI only, merged): design system,
  light default + Light/Dark switch, summary tiles (5 groups over GET /actions, 100 per state,
  2 s throttle), plain-language timeline, evidence cards, review comparison, inline validation.
  Ledger: outside_pinned_inventory in post-qualification-changes.json. Console tests now Vitest 103,
  Playwright 15. Status log has details.
- Console polish round 2 (2026-10-26, same branch/PR #8, merged): plain language for
  non-technical viewers (friendly resource/connector/tenant names, $ amounts, demo personas,
  review titles, Technical details folds), user menu, split sign-in, How SafeAct works card,
  hero + stepper + tabs on action detail, workflow flow, review cards. Demo personas: fixed
  principal ids in deploy/demo/bootstrap.py mapped by ui/src/demo/directory.ts (Phase A keeps
  them equal; real deployments would need a tenant-scoped names API). Tests: Phase A 964,
  PostgreSQL + broker 715, API E2E 14, Vitest 144, Playwright 15. Console-only Compose rebuilds
  need --no-deps (else the bootstrap re-runs). Screenshots: ~/safeact-evidence/console-polish-2.
- Publish readiness (2026-10-26, branch docs/publish-readiness, PR #9, merged): paired
  SafeAct vs B3 post-hoc analysis (docs/phase-e/qual-v1/paired/, 861/874 vs 841/874, safety equal),
  both agent scoring rules everywhere, README retry policy / Temporal-DBOS positioning / "What this
  doesn't prove" / first screen (screenshot + video placeholders), Stripe refund status shown as
  reported at response time (UI-only), demo refund $2,450.00. REVIEW-M58.md is sealed (never edit).
  Next: console comprehension round, then final screenshots (the video was later dropped). Tests: Phase A 967, Vitest 158.
- Guided desktop walkthrough (2026-10-26; baseline commit 0a13185 on ui/comprehension-round): `/demo` is the default entry,
  with problem → $2,450 refund → agent email → connected human decision + stale order
  → recorded results/limits. One visible reviewer sign-in; existing requester,
  investigator and scoped MCP identities remain separate. Existing console is a
  secondary inspection path. `ui/scripts/prepare_walkthrough.py --compose` reuses
  local mock cases without a reset and writes an ignored public ID/trace index;
  the email decision remains for the reviewer. See ui/README.md. No backend,
  API, lifecycle, migration, sealed report or paid-provider changes. Verification:
  Vitest 183, 21 browser checks (the three new walkthrough checks rerun after a
  document-title assertion fix), Phase A 967, real API/MCP walkthrough 1;
  preparation rerun checks 3; typecheck/lint/build and qualification disclosure
  checks pass.
- Live UI case check (2026-10-26): completed the prepared agent-email case through
  the real reviewer form. Email now SUCCEEDED with human_attested provenance;
  one attempt and independent mock ledger one send/one effect before and after,
  decision persisted after reload, standalone order still unresolved. No new
  action admission or resend. Corrected the email history's misleading provider
  lookup claim when the contract/evidence says no lookup. Vitest 184, lint/build
  pass; live browser checks have no page errors or serious accessibility issues.
  Local report/screenshots: ui/test-results/live-email-review-20261026/. Backend
  code unchanged; committed with the walkthrough baseline.
- UI comprehension round (2026-10-26, branch ui/comprehension-round from main 67f9b97, local
  commits, NOT pushed; one commit per fix): success worded by success_provenance; accurate
  post-escalation next-check text (README now states the lookup bound); approved proposals not
  flagged as changed; evidence summary by kind (demo order: 1 send + 66 lookups); phone layout
  (.stack-table cards, header, results card) with e2e/phone.spec.ts overflow check at 390px;
  Reviewer everywhere, Auto-updating, $2,450.00 / $4,900.00 total, technical state as small text,
  refund requester; .dockerignore excludes ui/public/demo-cases.json, so the guided walkthrough
  runs via Vite (:5173) against the Compose API, steps in README "Guided walkthrough"; evidence
  copies must equal their declared sources; Phase A runs the prep helper's tests. Tests: Phase A
  970, Vitest 208, Playwright 23. Scenario picker deferred (exploration in the session report).
- Final README (2026-10-27, branch docs/final-readme from main 6e5e19b): no demo video (author
  decision; milestone 5 labelled as reduced); README "Try it yourself" (demo-north-requester,
  Charge #1002 loses the response after applying, #1001/#1003/#1004 normal, up to $100,000.00;
  checked live) and "Reset and reseed" (down -v, up --build --wait, prepare_walkthrough.py
  --compose; preparation alone creates every case incl. the stale 409). Compose demo stack reset
  with the author's go (4 demo-only volumes); the 2026-10-26 email decision is gone and the email
  proposal is open again. Screenshots (light, 1440 px) in docs/images/ on the README first screen.
- Public-repo preparation (2026-10-27, branch chore/public-prep from main bd3584c): dated note on
  private history (top of this file and the status log; README points to it); live-doc links to
  the private CI run removed; fresh-clone.yml recorded as passing on every merge; v2 rule time
  labelled as private history; local-only references here replaced. Secret scan clean; no
  workflow uses secrets. Release asset recommended (not created): qual-v1/full/raw.jsonl.gz,
  full/qualify-full.log, diagnostics/raw.jsonl.gz. Licence: author to choose.
- License and closing notes (2026-10-27, branch docs/license-and-future from main 6817c5b): MIT
  LICENSE (Copyright (c) 2026 Deva Harsha Nuthakki); THIRD_PARTY_NOTICES.md for icon paths that
  follow Lucide (ISC) and Feather (MIT); README License section, a "complete as a portfolio
  project, not actively maintained" status line, and "What would close these gaps" (7 items, not
  planned work). Known intermittent e2e race: negative.spec.ts "duplicate submission" can reach
  /submit before the action page releases the retained request (failed once). Fixed in the test
  (branch test/fix-duplicate-submission-race): it waits for the release; 30/30 repeats passed.
- Public release v1.0.0 (2026-10-27, author's go; public repo DevaNuthakki/safeact, tag on
  a25bee2): three Release assets qual-v1-full-raw.jsonl.gz, qual-v1-full-qualify.log,
  qual-v1-diagnostics-raw.jsonl.gz, re-downloaded and matched to the sealed digests. Sealed
  packets (full/diagnostics REVIEW.md, outside-repo.json) left unchanged; the pointer is
  docs/phase-e/qual-v1/release-assets.md (branch docs/release-v1-links). README: CI badge,
  release links, first gap item "One laptop, no production operation". Tests: Phase A 970, Vitest 208.
- README "Running the tests" (2026-10-27, branch docs/running-the-tests): Python install
  (CI integration job's line), Phase A, Node 24 (nvm / nodejs.org / npx -p node@24), console
  checks, browser tests over deploy/compose.yaml, and two copies on one machine
  (COMPOSE_PROJECT_NAME + own ports). Every command run as written, with a separate
  project safeact-public-dev on 55433/56380; private stacks untouched.
- Walkthrough check from a second copy (2026-10-27, branch docs/walkthrough-second-copy):
  Compose stack as safeact-public-demo on 18080/18002, prepare_walkthrough.py --compose, Vite
  /demo with all 5 chapters on real cases, a fresh Charge #1002 refund went UNKNOWN
  (ambiguous_no_response) → VERIFYING → SUCCEEDED, 1 attempt, provider_verified. Fix:
  prepare_walkthrough.py defaults to :8080/:8002 (the first copy's stack), so the README's
  two-copies note now passes --api/--mcp and SAFEACT_API_URL with the copy's own ports.
- Observations and defects: docs/phase-e/observations.md.

## Open decisions and known limits
- STOP 2, calibration and the primary full run v1 are done (2026-10-25). Any further run is a
  diagnostic or a disclosed new freeze, each with the author's explicit go.
- The CI-only qualification amendment parses but has never been dispatched.
- Retained usefulness failures: F16 storm 1/8 useful completion; the payment deadline-outage case
  misses its achievable completion; D-F16-0002 (author, 2026-10-24): a fixed attempt budget can be
  spent inside a short outage, so SafeAct escalates with 0 effects (report + README state it; the
  candidate fix is not evaluated). They are results, not bugs to hide.
- Agents cannot start a second distinct action on one target (e.g. two partial refunds).
- Worker-side JWT/session binding is still open (closed for the human API path).
- Stated limits: runtime error-log attribution; SQL canonical-JSON enforcement; runtime
  credential/subject binding; asynchronous Stripe refund failure is out of scope (no webhooks).
- macOS fork risk: if a qualification worker crashes, check fork safety before blaming SafeAct;
  prefer Linux-container workers, and record where workers ran.

## How to work in this repo
- Run the full test suite before and after every change. Never finish with failing tests.
- Plan first for anything touching state transitions, locking, or authorization.
- Prefer completing a whole milestone slice over many tiny isolated steps.
- Explain design decisions briefly in commit messages so the author can defend them in interviews.
- Any change to pinned source changes chaos/source_qual_v1.json; re-pin only before STOP 2, and
  disclose it in the STOP 2 packet.
- After each step, update "Current status" above and add the detail to docs/phase-e/status-log.md.

## v1 milestones
1. Phase B vertical slice (admission → claim → fence → fake provider → outcome → crash recovery).
2. Three connector profiles + reconciliation.
3. Authenticated FastAPI + React console with version-bound human approval.
4. MCP server + bounded agent vs direct-tool agent vs deterministic caller.
5. Evaluation vs baselines, Docker Compose, CI, README with results table, demo video (v1:
   dropped by the author 2026-10-27; replaced by README screenshots and the guided walkthrough).
Full original scope: real payments sandbox, all 16 fault families, 1,000+ seeded scenarios vs
B0–B3, full observability.
