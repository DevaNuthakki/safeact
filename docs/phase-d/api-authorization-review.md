# Milestone 3 — API and authorization review (D0–D6)

Status: the API and authorization half of Milestone 3 is complete and
stopped for review **before any React UI code**. Local mock providers
and synthetic data only; production execution remains **not approved**
and the `public` schema registers no connector contract.

Blueprint anchors: §06 (approval boundary), §07 (journeys, access),
§08 (F14), §11 (A7 API side, A8 API side, A11).

## How to reproduce

```
docker compose --env-file .env -f deploy/compose.yaml up -d
.venv/bin/python -m alembic upgrade head
docker exec -i <postgres> psql -U safeact_dev -d safeact_dev -1 -f - < deploy/security/provision_local_admission_role.sql
.venv/bin/python -m unittest discover -s tests/phase_a -t . -p 'test_*.py'
SAFEACT_RUN_DB_INTEGRATION=1 SAFEACT_RUN_DISPOSABLE_DB_TESTS=1 SAFEACT_RUN_DB_SECURITY_TESTS=1 \
  SAFEACT_RUN_BROKER_TESTS=1 .venv/bin/python -m unittest discover -s tests/integration -t . -p 'test_*.py'
SAFEACT_RUN_ISOLATED_CLUSTER_TESTS=1 .venv/bin/python -m unittest discover -s tests/isolated -t . -p 'test_*.py'
SAFEACT_RUN_API_E2E=1 .venv/bin/python -m unittest discover -s tests/e2e_api -t . -p 'test_*.py'

# Demo (DEMO ONLY: synthetic accounts, mock providers)
.venv/bin/python -m deploy.demo.bootstrap      # idempotent rebuild of safeact_demo
.venv/bin/python -m deploy.demo.run_stack       # API on http://127.0.0.1:8000
```

CI (`.github/workflows/ci.yml`) runs all four on every push (since
M4.0 on 2026-10-19: on pushes to main, pull requests and manual dispatch), with
throwaway credentials and SHA-pinned actions.

## Where authority lives

The API process decides nothing it could get wrong in a way that
widens access. Every product call goes to one SECURITY DEFINER
function in PostgreSQL, as the API login role, which may EXECUTE only
`safeact_api_*` functions (no table privileges, no lifecycle
functions). Each function:

1. derives principal, tenant and roles from the session secret alone
   (`safeact_api_session`: hash lookup, absolute and idle expiry,
   revocation, disablement, CSRF hash for writes, required role);
2. applies the rule for its action (grants, visibility, eligibility,
   binding, separation of duties);
3. writes only through the existing guarded lifecycle functions.

So a request body cannot pick a tenant, actor, connector or provider
key, and a hidden UI control is never a permission.

### Endpoints × roles × tenant

| Endpoint | Who | Tenant / scope | Write guard |
|---|---|---|---|
| `POST /auth/login` | anyone | — | cross-site refused |
| `GET /auth/me` | signed in | own principal | — |
| `POST /auth/logout` | signed in | own session | CSRF |
| `GET /grants` | signed in | own grants only | — |
| `POST /actions` | requester | exact grant (connector, account, action, resource, contract) | CSRF |
| `GET /actions`, `GET /actions/{id}` | requester: own; investigator, reviewer: tenant | other tenant → 404 | — |
| `POST /actions/{id}/cancel` | the operation's requester, or an investigator | 404 outside visibility | CSRF |
| `POST /workflows` | requester with grants for **both** forward steps | own tenant | CSRF |
| `GET /workflows/{id}` | creator; investigator, reviewer | 404 otherwise | — |
| `POST /actions/{id}/proposals` | investigator | 404 outside tenant | CSRF |
| `POST /actions/{id}/verification-requests` | investigator | 404 outside tenant; inside contract lookup bounds | CSRF |
| `GET /proposals`, `GET /proposals/{id}` | investigator, reviewer | tenant | — |
| `POST /proposals/{id}/decision` | **human** reviewer, not proposer, not requester | 404 outside tenant | CSRF + binding digest |
| `GET /events` (SSE) | any role | tenant-visible changes | session re-checked each poll |
| `GET /healthz`, `GET /readyz` | anyone | — | — |

### Refusals (SQLSTATE → HTTP)

| SQLSTATE | Meaning | HTTP / code |
|---|---|---|
| SAT05 | unknown or invisible operation/proposal/workflow | 404 `not_found` |
| SAT17 | no / expired / revoked session | 401 `unauthenticated` (cookies cleared) |
| SAT18 | CSRF token missing or wrong | 403 `csrf_refused` |
| SAT19 | role required | 403 `forbidden` |
| SAT20 | identity rule (service password, service reviewer, demo principal outside demo) | 409 `identity_invalid` |
| SAT21 | action not granted | 403 `not_granted` |
| SAT22 | request key reused for a different request | 409 `request_conflict` |
| SAT23 | invalid request | 422 `invalid_request` |
| SAT24 | proposal already decided | 409 `already_decided` |
| SAT25 | self-review (own proposal or own request) | 403 `self_review` |
| SAT26 | not a permitted next action now | 409 `not_eligible` |
| SAT27 | retry needs a live reviewer approval (append guard) | (worker/reconciler path) |
| — | stale or expired binding (a recorded *result*) | 409 `stale_approval` + reason + current view |

## Sessions (0011)

- **Passwords**: bcrypt cost 12 via pgcrypto, a CHECK pins the cost;
  verified inside SQL.
- **Secrets**: the session secret and CSRF token are 32 random bytes
  from `gen_random_bytes`; only their SHA-256 is stored; the plaintext
  is returned once by login.
- **Cookies**: `__Host-safeact_session` (HttpOnly) and
  `__Host-safeact_csrf` (readable by the console), both `Secure`,
  `SameSite=Strict`, `Path=/`, no Domain. Writes send `X-SafeAct-CSRF`;
  the check runs in SQL. Middleware refuses cross-site writes
  (`Origin`, `Sec-Fetch-Site`) before any handler.
- **Expiry**: 8 h absolute, 30 min idle (append-only activity rows,
  at most one a minute). Logout appends a revocation.
- **Throttling**: `safeact_api_login` never raises on bad credentials;
  it records the attempt and returns `ok=false`, so the caller's
  commit keeps the failure row and the throttle actually triggers.
  5 failures in 15 min lock the account, 20 lock the source; a
  per-username advisory lock makes concurrent guesses count. Every
  path runs exactly one bcrypt check (a fixed dummy hash for unknown
  users) and every failure gets one byte-identical body.
- **No secrets in PostgreSQL logs**: the API role has
  `log_statement=none`, `log_min_duration_statement=-1`,
  `log_parameter_max_length[_on_error]=0`, `log_min_error_statement=panic`
  and sampling off; administrator seeding uses the same as `SET LOCAL`.
  Evidence: `tests/isolated/test_api_secret_logging.py` runs a
  PostgreSQL 14.23 cluster with `log_statement=all` and full parameter
  logging, drives login/whoami/logout (including refusals) as the API
  role, and greps the server log: no password, session secret or CSRF
  token; a control role without the settings does leak its password.

### The JWT-to-session binding gate

- **Closed for the human API path.** People authenticate with
  DB-verified sessions; the API credential alone cannot act as anyone,
  because every function derives the person from the session secret
  inside PostgreSQL (`test_api_sessions`
  `test_another_tenant_cannot_be_chosen_without_its_secret`,
  `test_api_review` `test_the_api_role_cannot_decide_without_a_reviewer_session`).
- **Re-scoped, still open for workers.** Workers, the reconciler and
  the advancer keep their per-role `session_user` identity (Phase B);
  binding a verified workload JWT to those sessions remains an open
  gate. It does not affect human authorization.

## Version-bound approval (0013, F14)

An investigator proposes `REVERIFY`, `PROTECTED_RETRY` or
`ATTEST_APPLIED`; the server checks the action is permitted now and
snapshots the binding — seq and state, payload hash, a digest of every
evidence row, the contract, the action (and attested reference) and an
expiry — as a SHA-256 binding digest. A reviewer approves by echoing
the digest their screen showed.

| F14 / A7 case | Result | Test |
|---|---|---|
| two reviewers at once | one decision, other 409 `already_decided`; one effect | `test_api_review` `test_two_reviewers_at_once_make_one_decision`; E2E `test_journey_3_review_with_stale_and_fresh_approval` |
| state changed since shown | 409 `stale_approval` (`operation_changed`), audited, undecided | `test_state_change_between_view_and_decision_is_stale` |
| new evidence since shown | 409 (`evidence_changed`) | `test_new_evidence_makes_a_proposal_stale`; E2E (reconciler lookups) |
| expired | 409 (`expired`) | `test_expired_proposal_is_stale` |
| out-of-date screen | 409 (`client_out_of_date`) | `test_a_screen_showing_an_old_binding_is_stale` |
| repeated click | same `decision_key` → `replayed`; new key → 409 | `test_a_repeated_click_returns_the_same_decision` |
| self-review (proposer / requester), every action | 403 `self_review` | `test_no_one_decides_on_their_own_proposal_for_any_action`, `test_no_one_decides_on_an_action_they_requested` |
| non-reviewer, anonymous, other tenant | 403 / 401 / 404 | `test_only_reviewers_decide`, `test_other_tenants_cannot_see_or_decide` |
| API role with forged/absent/non-reviewer secret | refused in SQL | `test_the_api_role_cannot_decide_without_a_reviewer_session` |

Approved actions stay under every earlier guard:
- **REVERIFY**: one extra post-escalation lookup beyond the contract's
  interval and horizon, consumed once; a reopen still needs decisive
  evidence.
- **PROTECTED_RETRY**: contracts listed in
  `safeact_contract_review_policies` (demo: `mock-crm v1`) retry only
  under a live approval for the current seq (append guard SAT27, on
  top of the retry basis and attempt budget). The approval performs
  VERIFYING → RETRY_ELIGIBLE itself.
- **ATTEST_APPLIED**: the reviewer's reference becomes
  `confirmed_applied` evidence with provenance `human_attested`. A
  CHECK allows human provenance only on applied effects, so a person
  can never supply proof of non-application (§04); an attestation
  cannot make FAILED_CONFIRMED or a retry legal
  (`test_an_attestation_never_makes_failure_or_retry_legal`).
  Successes are reported apart: `success_provenance` in the detail
  view, `succeeded_human_attested` in accounting.

## Submission, cancellation, live updates

- **Submission (0012)**: exact grants; same key → same operation,
  changed request → 409; duplicate JSON keys refused; the API picks the
  connector and canonicalizes. Workflow creation needs grants for both
  forward steps and records the principal as creator.
- **Cancellation (deviation 0012)**: `RECORDED → CANCELLED_BEFORE_CLAIM`,
  first transition only, no claim or attempt (append guard). Claim,
  relay and fence already exclude it; a repeated cancel-vs-claim race
  has exactly one winner; accounting keeps its own bucket (A5). Any
  later request is recorded as `CANCELLATION_REQUEST_REFUSED`; the
  response carries `undone: false`.
- **Live updates (0014)**: SSE over a feed keyed by
  `commit_xid < xmin(current snapshot)`, so late commits are delayed,
  never skipped; Last-Event-ID catch-up; session re-checked each poll.

## End-to-end journeys (A8, API side)

`tests/e2e_api/test_journeys.py` — real uvicorn, relay, Celery worker
over Redis, reconciler and advancer, mock providers with a
harness-only fault script (`deploy/demo/run_stack.py`), e2e-scaled
contracts. Assertions are on API answers, the database and the
harness effects ledger.

| Journey | Test | Shows |
|---|---|---|
| 1 Submit and follow | `test_journey_1_submit_follow_and_refresh` | SSE to SUCCEEDED; a new session re-reads identical state; one effect |
| 2 Investigate | `test_journey_2_timeout_after_success_is_verified_not_redone` | UNKNOWN then lookup-verified success, one attempt, one effect |
| 2 Investigate | `test_journey_2_late_commit_stays_unknown_and_is_listed` | empty lookups never become failure; escalated, listed as unresolved; nothing resent |
| 3 Review | `test_journey_3_review_with_stale_and_fresh_approval` | concurrent reviewers → one decision; attested success; a proposal outrun by new evidence is refused |
| 3 Review | `test_journey_3_protected_retry_after_review` | approval-gated retry; second attempt succeeds; one effect |
| Denied tenant | `test_denied_tenant_access` | 404 on read and cancel; feed never shows it |
| Duplicate submission | `test_duplicate_submission_is_one_operation_and_one_effect` | four concurrent posts → one operation, one effect |
| Connection interruption | `test_connection_interruption_catches_up_from_last_event_id` | drop mid-stream; reconnect with Last-Event-ID catches the missed change |

## Review decisions (applied in 0015)

The review of D0–D6 settled the open points; migration
`0015_review_followups` applies them.

1. **Expired retry approval → FAILED_CONFIRMED, named.** When a
   reviewed contract's retry is not approved before the investigation
   deadline, the operation ends FAILED_CONFIRMED with the reason
   `retry_approval_expired` ("not done; you can resubmit"), reported
   apart from a permanent provider rejection (`safeact_outcome_reasons`,
   `Accounting.failed_retry_approval_expired`). The append function
   accepts that reason only for VERIFYING → FAILED_CONFIRMED on a
   reviewed contract, after the deadline, with no live approval, and
   with **system-provenance** proof of non-application for every
   attempt (`test_api_review_followups` `ApprovalExpiryTests`).
2. **Direct verification requests (blueprint 06).** An investigator
   may `POST /actions/{id}/verification-requests` without approval:
   lookup-only, for escalated work, inside the contract's bounds —
   after the post-escalation check interval since the last lookup,
   before the horizon, at most three per escalation — checked when
   requested and again when the reconciler takes it. Inside the bounds
   the lookup would run anyway; the request is consumed by it and
   recorded as its cause, so it can never make lookups more frequent
   than the contract allows. A reopen still needs decisive evidence.
   Only beyond the bounds is a reviewer-approved `REVERIFY` proposal
   needed (`DirectVerificationTests`).
3. **Review policy in the binding.** `safeact_contract_review_policies`
   is insert-only (write guard) and keyed to `(connector_id, version)`;
   the binding digest now covers the policy row in force, so a policy
   appearing after a proposal makes it stale (`PolicyBindingTests`).
4. **Known limits accepted**, with one mitigation:
   `idle_in_transaction_session_timeout = 60s` on every application
   login role (API, worker, recoverer, advancer). No application
   transaction waits on a provider while open, so normal work never
   comes near it (`IdleTimeoutTests`).

Earlier plan deviations, all accepted: no separate
`retry_awaiting_approval` reason (the operation waits in VERIFYING);
the review policy is a table rather than a contract column;
time-scaled demo/E2E contracts (`deploy/demo/profiles.py`, tested to
match every non-timing column); `run_stack.py` in D6; the one
frozen-module allowlist edit for `safeact_demo`.

## Known limits

- **Per-account lockout** lets someone lock out a known user on
  purpose (accepted for the demo). Production would add per-source
  throttling first, step-up/CAPTCHA and notifications.
- **Per-source throttling behind a proxy**: through the Vite dev
  proxy every browser shares one source address.
- **Human-attested success is a trust extension**: two-person rule,
  recorded provenance and separate reporting; never usable for
  non-application.
- **Feed horizon**: a long-running transaction anywhere holds live
  updates back (delayed, never lost). An abandoned *application*
  transaction is ended after 60 s (`idle_in_transaction_session_timeout`),
  so it cannot delay live updates longer than that; administrator or
  other sessions are not bounded by it.
- **Stream cap is per process** (in-memory), not cluster-wide.
- **Full-log scans** for lists and the feed (derived current-state
  projection still deferred, not reduced).
- **Worker-side JWT binding** remains open (above).
- **Canonical JSON** is produced by the API (Python); SQL checks it is
  a JSON object and, for workflows, that the values match, but does not
  enforce the canonical byte form (the existing Phase B limit).
- **Local transport**: no TLS on the local stack; passwords reach
  PostgreSQL as bound parameters over localhost.

## Deferred, not reduced

React console (D7–D9), Playwright E2E and accessibility, exit evidence
(D10); MCP and agent evaluation, F15 and A7 at the MCP boundary
(Milestone 4); Stripe sandbox adapter and F16 (rest of Phase D);
derived current-state projection (before the evaluation milestone).
