# Milestone 3 exit evidence — authenticated product and review console

Status: Milestone 3 complete. Authenticated FastAPI product and React +
TypeScript review console over the durable lifecycle, with
version-bound human approval, for the three **mock** connector
profiles. Local disposable/demo schemas only; production execution
remains **not approved** and deny-only (the `public` schema registers
no connector contract and holds no principal).

Blueprint references: §06 (approval boundary), §07 (three journeys,
access, usability, frontend verification), §08 (F14), §11 (A7 at the
API, A8, A11).

Companion: [`api-authorization-review.md`](api-authorization-review.md)
(endpoint × role × tenant matrix, SQLSTATE map, session handling, log
evidence, review decisions).

## What this does and does not claim

Claimed, within the tested mock contracts and local stack:

- the three §07 journeys work end to end in a real browser and over
  HTTP, with every rule enforced in PostgreSQL by the API's restricted
  role — a hidden button is never the permission (**A8**);
- tenant isolation and human-only, independent approval at the API
  boundary (**A7, API side**);
- one valid version-bound decision under stale and concurrent approval
  (**F14**);
- zero observed duplicate effects in every end-to-end journey (counted
  by the harness ledger);
- the JWT-to-session gate is closed for the human API path.

Not claimed: A7 at the MCP boundary and F15 (Milestone 4); F16 and the
Stripe sandbox adapter (rest of Phase D); production SSO, TLS, or any
security certification; worker-side identity binding (still open);
behaviour under load (Phase E).

## Reproduce

```
docker compose --env-file .env -f deploy/compose.yaml up -d
.venv/bin/python -m alembic upgrade head          # then the provisioning script (psql -1)
.venv/bin/python -m unittest discover -s tests/phase_a -t . -p 'test_*.py'
SAFEACT_RUN_DB_INTEGRATION=1 SAFEACT_RUN_DISPOSABLE_DB_TESTS=1 SAFEACT_RUN_DB_SECURITY_TESTS=1 \
  SAFEACT_RUN_BROKER_TESTS=1 .venv/bin/python -m unittest discover -s tests/integration -t . -p 'test_*.py'
SAFEACT_RUN_ISOLATED_CLUSTER_TESTS=1 .venv/bin/python -m unittest discover -s tests/isolated -t . -p 'test_*.py'
SAFEACT_RUN_API_E2E=1 .venv/bin/python -m unittest discover -s tests/e2e_api -t . -p 'test_*.py'
cd ui && npm ci && npm test && npm run build && npx playwright install chromium && npx playwright test

# Demo (DEMO ONLY: synthetic accounts, mock providers, demo-scaled contracts)
.venv/bin/python -m deploy.demo.bootstrap
.venv/bin/python -m deploy.demo.run_stack            # API :8000
cd ui && npm run dev                                 # console :5173; sign-ins in .demo-credentials
```

Result at exit: **Python 817** (342 Phase A, 450 PostgreSQL + broker,
17 isolated, 8 API end-to-end) and **console 32** (23 Vitest component
tests, 9 Playwright browser specs), all passing, no skips with the
flags above. CI (`.github/workflows/ci.yml`) runs every one of them on
each push with throwaway credentials and SHA-pinned actions. (Since
Milestone 4, step M4.0, on 2026-10-19: CI runs on pushes to main, on pull requests and on
manual dispatch, once per change.)

## Components

| Concern | Location |
|---|---|
| API (FastAPI factory, auth, actions, review, SSE) | `api/` |
| Product SQL calls (typed) | `storage/product_api.py` |
| Identity seeding, no-secret logging, idle timeout | `storage/identity_admin.py` |
| Shared schema builder (fixture and demo) | `storage/lifecycle_environment.py` |
| Migrations 0011–0015 (frozen, digest-pinned) | `storage/lifecycle_sql/v0011.py` … `v0015.py` |
| Demo bootstrap, scaled contracts, stack runner, browser-test stack | `deploy/demo/{bootstrap,profiles,run_stack,e2e_server}.py` |
| Console | `ui/src` (client, session, live updates, pages) |
| API end-to-end journeys | `tests/e2e_api/test_journeys.py` |
| Browser journeys and accessibility | `ui/e2e/*.spec.ts` |

## Migrations

| Revision | Adds | New SQLSTATEs |
|---|---|---|
| 0011_identity_sessions | principals, bcrypt credentials, role/action grants, hash-only sessions, login throttling, `safeact_api_session` | SAT17–SAT20 |
| 0012_product_admission | authorized submission and workflow creation, tenant-scoped reads, my grants, cancellation (`CANCELLED_BEFORE_CLAIM`) | SAT21–SAT23 |
| 0013_review | contract review policies, proposals, decisions, human-attested provenance, approval-gated retry, success by provenance | SAT24–SAT27 |
| 0014_change_feed | `commit_xid` on audit rows, `safeact_api_changes` | — |
| 0015_review_followups | expired-approval reason, direct verification requests, policy in the binding, outcome reasons | — |

Each has an upgrade/downgrade/upgrade round trip on a throwaway
database, a downgrade that refuses while history depends on it, and an
autogenerate guard (columns too, for 0014). The frozen-module rule was
amended once, by your decision: `v0003._schema` admits the exact name
`safeact_demo`; every digest is unchanged.

## Journeys (§07) → tests

| Journey | Browser (`ui/e2e`) | HTTP (`tests/e2e_api`) | Component / SQL |
|---|---|---|---|
| Submit and follow | `journey 1: submit once, follow live, recover after a refresh` | `test_journey_1_submit_follow_and_refresh` | `Submit.test.tsx`, `test_api_actions` `SubmissionTests` |
| Investigate uncertainty | `journey 2: investigate a timeout after success and a late commit` | `test_journey_2_*` (both) | `ActionDetail.test.tsx`, `test_api_actions` `ReadTests` |
| Review a proposal | `journey 3: propose, refuse a stale second decision, approve once`; `journey 3: a screen left open while evidence changes cannot approve` | `test_journey_3_*` (both) | `ProposalDetail.test.tsx`, `test_api_review` |

## A8 — under denied access, stale state and reconnection

| Condition | Browser | HTTP |
|---|---|---|
| Denied tenant access | `denied tenant access: another tenant's action is not found` | `test_denied_tenant_access` |
| Stale approval | both journey-3 specs | `test_journey_3_review_with_stale_and_fresh_approval` |
| Duplicate submission | `duplicate submission: a double click and a refresh are one operation` | `test_duplicate_submission_is_one_operation_and_one_effect` |
| Connection interruption | `connection interruption: live updates recover and catch up` (live channel cut, API outage) | `test_connection_interruption_catches_up_from_last_event_id` |
| Accessibility | `every page has no serious accessibility violations` (axe WCAG 2.1 A/AA); `keyboard only: skip link, sign-in and submit` | — |

## A7 (API side) and F14

See the tables in the authorization review. In short:
- **Tenant isolation:** every read, write and feed is scoped in SQL from the session; other tenants get 404 (`test_api_actions` `ReadTests`, `test_api_review` `HumanOnlyApprovalTests`, `test_api_events`).
- **Human-only approval:** only a human reviewer decides, never on their own proposal or request (SAT25), and the API role cannot decide without a reviewer's session.
- **F14:** one decision per proposal under the operation lock. A stale, expired or out-of-date binding is a recorded 409, and a repeated click replays the stored decision (`test_api_review` `StaleAndConcurrentTests`).

## Deliberate deviations (each guarded inside the protected append function)

| Pair / rule | Why | Guard |
|---|---|---|
| RECORDED → CANCELLED_BEFORE_CLAIM (0012) | §07 cancellation requests | first transition only, no claim or attempt; any later request is recorded, never an undo |
| VERIFYING → RETRY_ELIGIBLE needs approval on reviewed contracts (0013) | §06 human authorization of a documented safe action | live approval bound to the current seq (SAT27), on top of retry basis and budget |
| `human_attested` provenance (0013) | §06 "human review may resolve evidence" | CHECK: applied effects only; never usable as proof of non-application (§04); reported apart |
| `retry_approval_expired` reason (0015) | name "not done; resubmit" apart from a provider rejection | reviewed contract, deadline passed, no approval, system proof for every attempt |
| Direct verification requests (0015) | §06 `request_verification` without approval | lookup-only, inside the contract's interval/horizon, three per escalation; REVERIFY approval beyond |

## The JWT-to-session binding gate

**Closed for the human API path:** people authenticate with sessions
verified in PostgreSQL, and every product function derives the person
from the session secret alone. **Re-scoped and still open for
workers** (per-role `session_user` identity, Phase B).

## Known limits

Accepted at review (details in the authorization review):
- per-account lockout can be abused to lock out a known user;
- per-source throttling sees one address behind the dev proxy;
- human attestation is a trust extension;
- feed delay from long transactions is bounded to 60 s for application roles (`idle_in_transaction_session_timeout`);
- the stream cap counts per process;
- lists and the feed use full-log scans;
- canonical JSON is produced by the API, not enforced in SQL;
- there is no TLS on the local stack.

Also:
- **Demo and E2E contracts are time-scaled** (`deploy/demo/profiles.py`), proven equal to the real profiles except for timings.
- **The console is served by the Vite dev server**, with a same-origin proxy that sends browser navigations to the app. A production bundle server is part of the Compose work in Milestone 5.
- **Browser tests use Chromium only.**

## Known items under observation

Recorded so they can be investigated if they recur. They are not claimed fixed.

### Unreproduced `setUpClass` error in `test_api_review.ProvenanceGuardTests`

- **When:** 2026-10-19 at 21:17 UTC (23:17 CEST).
- **Where:** a local macOS run against the dev compose PostgreSQL 17.11. The working tree was
  uncommitted at the time, in the middle of the 0015 review follow-ups.
- **Command:** only `SAFEACT_RUN_DB_INTEGRATION=1 SAFEACT_RUN_DISPOSABLE_DB_TESTS=1` were set, with
  three modules in one process:
  `.venv/bin/python -m unittest tests.integration.test_api_review_followups tests.integration.test_api_review tests.integration.test_api_sessions`
- **Result:** `ERROR: setUpClass (tests.integration.test_api_review.ProvenanceGuardTests)`, after
  60 tests in 394.9 s. That is about 1.7× slower than the clean rerun, so the machine was likely
  under load.
- **The traceback was lost.** The output was piped through a `grep` filter that kept only summary
  lines, so the exception type is unknown.
- **Reruns:**
  - the class alone passed: 2 tests in 5.5 s;
  - the same three modules together passed: 62 tests in 227.6 s.

  It has not recurred since, locally or in CI.
- **Suspects (unconfirmed):**
  - The class shares its setup with `ReviewCase`. Each class runs a full `rebuild()` of a new
    disposable schema under `lock_timeout 5s`, right after the previous class's cleanup ran
    `pg_terminate_backend`, which is asynchronous. A backend that has not finished exiting can hold
    a lock.
  - `connect_timeout 5` on each of the class's connections, under load.
  - Six sign-ins with bcrypt cost 12 each.
- **Diagnostics added in M4.0:** `api_fixture.signed_in` now reports the HTTP status and error code
  instead of a bare "sign-in failed".
- **If it recurs:** keep the unfiltered output, and note the load, the PostgreSQL log around the time
  and which classes ran first.

## Deferred, not reduced

- MCP server, bounded agent, F15, A7 at the MCP boundary: Milestone 4.
- Stripe sandbox adapter, F16: rest of Phase D.
- Compose packaging of API + console, observability: Milestone 5.
- Derived current-state projection: before the evaluation milestone.
- Worker-side JWT binding: open gate.
