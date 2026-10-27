# Customer-support improvements: development verification

Work remains uncommitted on `feature/m5-sandbox-qualification`. This is a product
and correctness slice, not a completed qualification milestone or a production
release. No Stripe or paid model API was used. Existing raw development packets,
source archives, frozen corpus files and 0003–0020 SQL were not rewritten.

## Product behavior

- Create the fixed replacement workflow from exact granted customers and cases.
  Preserve the whole pending request before admission, scoped to principal and
  tenant, and recover its identity after a lost reply and browser reload.
  Both workflow and single-action forms bind resource choices to stable grant
  identities, so permission-list refreshes cannot redirect the selected target.
  Single-action submission also requires an explicit resource selection instead
  of interpreting the empty placeholder as the first grant.
- Follow charge, replacement order and conditional refund as separate steps.
  Inspect their recorded timeline, evidence references, provider verification
  versus reviewer attestation, and the next safe step. An uncertain order
  withholds compensation; a confirmed refund does not become a completed order.
- Run [`examples.replacement`](../../../examples/replacement.py) against the
  local authenticated API. Retain the business request key, use exact replay
  after uncertainty, and stop for investigation rather than generating new work.
- Reproduce [workflow](../workflow-outcomes.md) and
  [recovery](../recovery-outcomes.md) customer-outcome summaries from saved rows.
  Keep the 2/2 achievable workflow units distinct from five requested workflows,
  and retain recovery's 14/22 completion and the original F16 storm's 1/8 result.
  B0–B3 comparisons, full qualification and load results remain pending.

The [walkthrough](../customer-support.md) shows a clean order, a permanently
rejected order followed by a separate refund, and an uncertain order without a
refund. Existing demo environments need a deliberate bootstrap/restart for the
new grant and function definitions; validation used disposable environments and
did not reset the author's active demo.

## Correctness findings

1. Replaying a workflow key with fee 501 instead of 500 returned the original
   workflow as an identical replay. The stored charge was not changed and no
   second charge was created. Revision **0021_workflow_replay** now compares the
   immutable original charge as well as the order/inverse plan. Both the API
   and internal SQLAlchemy path refuse changed charge inputs. Existing rows
   replay after upgrade without a backfill, and owner/ACL checks passed.
2. A concurrent identical API admission returned HTTP 500 on the provider-key
   unique index. The API E2E test reported OK because its worker-thread failure
   was not propagated. Revision **0022_admission_race** handles both unique
   indexes, then still requires immutable identity and requester comparison.
   Internal admission uses equivalent handling. Futures now propagate all
   duplicate-submission failures to the parent test. Synchronized API and
   internal batches check every response and one operation/audit/outbox bundle;
   different payloads still conflict.

The [design](../../phase-e/workflow-replay-binding.md) records the changes before
implementation. Both migrations were applied to the local development database,
and the existing local admission-role provisioning script was rerun. Constraints,
lifecycle transitions and permissions were not loosened.

## Verification

All final checks passed:

| Suite | Passed |
|---|---:|
| Phase A unit tests | 758 |
| PostgreSQL and broker integration, including isolated Redis | 547 |
| Isolated PostgreSQL security | 21 |
| Real HTTP API journeys | 9 |
| Scripted-agent harness | 2 |
| **Distinct Python tests** | **1,337** |
| Console unit tests | 37 |
| Browser journeys | 14 |
| **Console tests** | **51** |

Typecheck, lint, build, documentation links and diff checks passed. The final
database/broker pass completed in 724.2 seconds without skips, after the earlier
541-test intermediate-source pass. Final API journeys have no hidden thread
exception or server-error log. Browser checks cover the real workflow API,
separate original/refund identities, lost admission reply followed by reload,
request conflicts, evidence rendering and accessibility on the new pages.

These totals count each test once; earlier reruns are retained for diagnosis
and do not increase them. [Regression checks](regression-checks.json) records
the final counts and original log hashes.

Earlier development attempts are retained alongside the final outcomes. These
include the first example test's missing-CSRF setup, the changed-fee
counterexample, the API suite's hidden thread error despite an OK summary, and
the new migration's initial CREATE-versus-CREATE-OR-REPLACE mistake. That first
migration draft failed in a disposable schema before application to the local
development database. Tests were corrected or strengthened; no assertion or
timeout was weakened to obtain a pass.

## Retained artifacts

- Actual final-source console screenshots: [completed replacement](replacement-completed.png),
  [confirmed refund](refund-confirmed.png), and [unresolved replacement](replacement-unresolved.png).
- [Source archive](source.tar.gz) and [source hashes](source-sha256.json): 386
  runtime/test/dependency files and saved-result fixtures as an overlay on the
  recorded base commit. Four unchanged base files containing intentional scanner
  fixtures or multi-line digest literals are referenced by hashes instead of
  duplicated; restore them from the base checkout. Every archive entry was
  checked against its recorded hash, and archived source passed the text scan.
- [Log snapshot inventory](log-snapshots.json): original local log hashes and
  redaction counts. Shared log copies replace 64-hex strings, including provider
  correlations and content digests. Original local files remain unchanged.
  Test counts, error types and observed outcomes are preserved. No credentials
  or local environment files are included.

The source archive and screenshots document this uncommitted development
version. They do not freeze the qualification corpus or its scoring.

## Boundaries

Browser session storage only retains a pending request within the tab's lifetime;
integrators must durably retain business intent and request identity. Different
authorized keys can still mean different business actions. Timeline reads can
refresh at different times and disclose missing evidence. Observation counts
are not counts of human reviews performed. Provider activity is not a measured
comparison of overhead, and mock cost is not Stripe pricing or money saved.

The original documented production boundary, canonical-JSON SQL limitation and
runtime credential/subject-binding limitation remain. This work does not
establish universal exactly-once execution or production readiness.

**FINAL BIG QUALIFICATION RUN: NOT STARTED.** Finish the remaining drivers,
baselines and scoring review, freeze the corpus, complete STOP 2, announce the
run prominently and wait for the author's explicit go.
