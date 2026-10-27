# Milestone 4 exit evidence — MCP server and bounded agent evaluation

**Status:** Milestone 4 complete. SafeAct now has:
- a guarded MCP server for agents, with identity, tenant and scope derived in PostgreSQL on every
  call;
- no approval tool for agents;
- A7 at the MCP boundary and F15, tested;
- the blueprint §06 agent evaluation: the same model with guarded versus direct tools, plus a
  deterministic caller, on frozen held-out scenarios, with costs and every failure published.

Local, mock providers and synthetic data only. Production execution remains **not approved** and
deny-only: `public` registers no contract and holds no principal or agent key.

**Blueprint references:**
- §06: agent behaviour, MCP, approval boundaries, required agent evaluation;
- §08: F15;
- §09: distinct outcomes, orphan effects, safety vs usefulness, costs;
- §11: A7 at the MCP boundary; A9 for the MCP-client trials;
- §12: evidence pack, including agent prompts and tool versions.

**Companions:**
- [`mcp-authorization-review.md`](mcp-authorization-review.md): the boundary, tool × scope matrix,
  refusal codes, key handling, F15 table, log evidence and review decisions.
- [`agent-evaluation-design.md`](agent-evaluation-design.md): the design, calibration history and
  disclosures.
- [`agent-eval-v1/README.md`](agent-eval-v1/README.md): the results, every failure, costs and model
  lifecycle.

## What this does and does not claim

**Claimed, within the tested mock contracts and the local stack:**
- **A7 at the MCP boundary:** tenant isolation, own-operations-only visibility for agents, and
  scoped keys. An agent can never approve: there is no approve tool, the MCP role has no decision
  function, and a service principal can never hold the reviewer role or scope. All enforced in
  PostgreSQL.
- **F15:**
  - changing the key or intent cannot produce a second effect (derived request keys and SAT28);
  - a changed payload is a conflict (SAT22);
  - extra identity or key arguments are refused;
  - self-approval attempts do nothing.
- **Provider text is untrusted input:** returned only in a marked, sanitized field and never read
  by a decision.
- **Agent evaluation (held-out):** across 72 trials per arm, the guarded model executed **0** unsafe
  effects. The same model with direct tools executed **28**, made 12 false claims and caused 6
  orphan effects.

**Not claimed:**
- the agent is better than deterministic logic (it isn't here: 71 vs 72);
- behaviour with real providers, other models or other prompts;
- rates or probabilities (6 trials per scenario);
- a robust direct integration baseline (B3; Milestone 5);
- F16 and the sandbox adapter (rest of Phase D);
- worker-side identity binding (still open);
- production TLS or SSO.

## Reproduce

```
docker compose --env-file .env -f deploy/compose.yaml up -d
.venv/bin/python -m pip install -r requirements-db.lock -r requirements-broker.lock -r requirements-api.lock \
  -r requirements-mcp.lock -r requirements-agent.lock -r deploy/security/requirements-identity.txt
.venv/bin/python -m alembic upgrade head          # 0016_agent_keys; then the provisioning script (psql -1)
.venv/bin/python -m unittest discover -s tests/phase_a -t . -p 'test_*.py'
SAFEACT_RUN_DB_INTEGRATION=1 SAFEACT_RUN_DISPOSABLE_DB_TESTS=1 SAFEACT_RUN_DB_SECURITY_TESTS=1 \
  SAFEACT_RUN_BROKER_TESTS=1 .venv/bin/python -m unittest discover -s tests/integration -t . -p 'test_*.py'
SAFEACT_RUN_ISOLATED_CLUSTER_TESTS=1 .venv/bin/python -m unittest discover -s tests/isolated -t . -p 'test_*.py'
SAFEACT_RUN_API_E2E=1 .venv/bin/python -m unittest discover -s tests/e2e_api -t . -p 'test_*.py'
SAFEACT_RUN_AGENT_HARNESS=1 .venv/bin/python -m unittest discover -s tests/e2e_agent -t . -p 'test_*.py'
cd ui && npm ci && npm test && npx playwright test

# Demo (DEMO ONLY): API :8000, guarded MCP 127.0.0.1:8002/mcp; agent keys in .demo-credentials
.venv/bin/python -m deploy.demo.bootstrap && .venv/bin/python -m deploy.demo.run_stack
```

**Result at exit:** **Python 925** (413 Phase A, 481 PostgreSQL + broker, 21 isolated, 8 API E2E,
2 agent harness) and **console 32** (23 Vitest, 9 Playwright), all passing locally.
- **CI** (`.github/workflows/ci.yml`) runs every suite on each pull request and push to main, with
  scripted models only. It never calls a real model.
- **Paid runs** are reproducible with the commands in the results README (they need a key file and
  the paid flag).

## Components

| Concern | Location |
|---|---|
| Agent keys, per-call identity, SAT28 (frozen) | `storage/lifecycle_sql/v0016.py`, `deploy/migrations/versions/0016_agent_keys.py` |
| Agent SQL calls (typed) | `storage/agent_api.py` |
| Guarded MCP server, tools v2, untrusted projection | `mcp_server/server.py`, `tools.py`, `untrusted.py`, `settings.py` |
| Direct mock-only tools (baseline arm) | `mcp_server/direct/server.py` |
| Agent runner, models, budgets, key file | `mcp_server/agent/` |
| Scenarios, faults, harness, scoring, report, combine, secret scan | `deploy/evaluation/agent_v1/` |
| Frozen manifests, spend ledger | `deploy/evaluation/agent_v1/manifest_{dev,heldout}.json`, `spend.jsonl` |
| Results (raw, unedited) | `docs/phase-d/agent-eval-v1/` |

## Migration

| Revision | Adds | New SQLSTATE |
|---|---|---|
| 0016_agent_keys | agent keys (hash only, scopes ⊆ {submit, investigate}, ≤ 30 days), revocations, per-call sessions, the `safeact_mcp_*` functions; replaces `safeact_api_session` (key re-check, scope narrowing) and `safeact_api_visible` (agents see only what they requested) | SAT28 conflicting operation (agent path only) |

**Checks:**
- upgrade/downgrade/upgrade round trip; the downgrade refuses while any key exists;
- frozen digest;
- the provisioning script pins 0016;
- privileges tested in both directions;
- isolated PostgreSQL 14.23 log test: no agent key in a fully logged server log, with a loud control.

## A7 at the MCP boundary and F15 → tests

| Check | Tests |
|---|---|
| Transport gate (missing or malformed bearer, session secret, cookie) → 401 | `test_mcp_authorization` |
| Forged, expired, revoked or disabled key → unauthenticated, no rows | `test_mcp_authorization`, `test_agent_keys` |
| Tenant: another tenant's operation looks exactly like a missing one | `test_mcp_authorization` |
| Own-only visibility inside the tenant | `test_agent_keys`, `test_mcp_authorization` |
| Scopes narrow roles; grants bound every tool | both |
| F15 key change: same intent is the same operation; new intent → SAT28 (concurrent race admits one); changed payload → SAT22; extra key or identity arguments → invalid | both |
| F15 self-approval: no approve tool; unlisted names refused; agent key at the decision API → 401; MCP role cannot execute decide; only a human reviewer decides | both |
| One parse per request; duplicate JSON keys resolve to one value (both protocol eras) | `test_mcp_authorization`, Phase A AST test |
| Untrusted provider text kept apart and inert | `test_mcp_authorization`, `test_mcp_tools` |

## Agent evaluation

**Design:**
- **Arms:** a guarded model (`gpt-4.1-mini-2025-04-14` with SafeAct MCP tools `safeact-guarded-v2`);
  a direct model (the same model and prompt with `direct-mock-v1` tools that call the mocks
  directly); and a deterministic caller on the guarded tools.
- **Scenarios:** 12 frozen held-out scenarios covering timeout after success, pending work, stale
  lookup, key expiry (a true F09 under its declared contract), denied access, malicious tool text,
  attempted self-approval, and no-fault controls.
- **Ground truth:** the harness effects ledger and the database.

| Arm (72 trials each) | Correct, strict rule (v1) | Correct, rule frozen before the held-out run (v2) | Unsafe executed | Orphan effects | False claims |
|---|---|---|---|---|---|
| guarded model | 66/72 | 71/72 | 0 | 0 | 0 |
| direct model | 39/72 | 39/72 | 28 | 6 | 12 |
| deterministic | 72/72 | 72/72 | 0 | 0 | 0 |

**The two rules:**
- v1 accepts only each scenario's listed report.
- v2 also accepts either escalation status where escalation is expected, for every arm,
  and never counts it as success.
- **Timing:** v2 was adopted after the paid dev calibration and committed at 17:37 UTC
  on 2026-10-20. The held-out run started at 18:30 UTC that day. The commit time comes from
  the private development history; the run id and the held-out manifest that freezes v2 are
  in this repository.
- **Which trials change:** all 5 trials whose verdict differs are one scenario, AG1-H07.

**What the deterministic caller shows:** its 72/72 shows the value of the constrained
tools, not better model reasoning.

**Scope:**
- mock providers with injected faults;
- one model snapshot;
- 6 trials per scenario, so these are observed counts, not rates;
- the trial count was limited by the $4 budget.

**Runs and costs:**
- The original run was trials 1–3. The extension (trials 4–6) was pre-registered before any of its
  results existed, and the combined view refuses mixed manifests or repeated trials.
- **Cost:** all paid runs together cost $0.3918 of the $4 cap (3 dev calibrations and 2 held-out
  runs).
- **Model lifecycle:** `gpt-4.1-mini-2025-04-14` shuts down on 2027-01-14 (replacement
  `gpt-6-luna`). The results cannot be rerun on this snapshot after that date. The harness is
  model-agnostic.

**Full results and every failure:** [`agent-eval-v1/README.md`](agent-eval-v1/README.md).

## Deliberate deviations and disclosures

| Item | Why | Guard |
|---|---|---|
| Replaced `safeact_api_session` / `safeact_api_visible` (0016) | agent identity per call through the existing product functions | human path unchanged (the whole human suite passes); a service principal has no non-key session |
| SAT28 on the agent path | F15: an agent can't get a second effect by changing its intent or key | known limit: no second distinct action on one target (e.g. two partial refunds); that needs the human path |
| Tools v2, after the dev calibration | v1 status waits woke on internal UNKNOWN ↔ VERIFYING flips | disclosed; made before any held-out result |
| Scoring v2, after the dev calibration | either escalation status is correct where escalation is expected | a rule, not a list of ids; every run shown under both rules; disclosed |
| Effect attribution fix, after the v2 dev calibration | a reference reused across providers was miscounted | effects counted when applied; orphan effects reported; the one affected trial corrected in `CORRECTIONS.md` |

## Known limits

**Accepted at the M4.3 review (details in the authorization review):**
- one session row per tool call;
- duplicate JSON keys resolve to the last value: one parse per request, consistent everywhere;
- a well-formed bearer can list the (public, pinned) tools;
- status waits poll PostgreSQL;
- no TLS on loopback.

**Also:**
- **Evaluation:** small samples; mock providers; one snapshot. Latency isn't comparable across arms
  (in-process vs HTTP and PostgreSQL). "Blocked" is attempts minus executed.
- **Lost log:** the first dev calibration's console output was filtered and not saved. Every later
  run's unfiltered log is committed and clean.
- **Worker-side JWT binding** is still open (Phase B gate).

## Known items under observation

Not claimed fixed.

- **Unreproduced `setUpClass` error in `test_api_review.ProvenanceGuardTests`:** seen 2026-10-19,
  local; details in [`milestone-3-exit-evidence.md`](milestone-3-exit-evidence.md). It has not
  recurred, and the fixture now reports status and error code.
- **One local Playwright run failed 3 of 9 specs (2026-10-20):** it ran right after the Python
  suites; two reruns passed. Details in the authorization review.
  - Each browser run now keeps its own artifacts.
  - The e2e server refuses a shared Redis database or port, and records a run context.
  - It has not recurred.

## Deferred, not reduced

- **Milestone 5:** evaluation against baselines B0–B3 (including a robust direct integration),
  Compose packaging, observability.
- **Rest of Phase D:** Stripe sandbox adapter, F16.
- **Before the evaluation milestone:** derived current-state projection.
- **Open gate:** worker-side JWT binding.
- **Later:** repeating the agent evaluation on the replacement snapshot.
