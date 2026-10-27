# Remaining M5.9 review and agent drivers

Implementation notes from the existing protected API/MCP paths, 2026-10-22.
The development drivers now run through the real API/MCP processes. Earlier
API/MCP integration tests remain separate evidence; their pass counts do not
close these qualification cells. This is not the frozen qualification run,
and no commit is authorized.

## Shared boundary and observation work

Implementation sequencing: boundary setup and sign-in occur in `prepare`, outside
the timed window. A runner hook after admission drives the review/agent exchanges
before ordinary settling. Raw `boundary` events contain only allowlisted response
fields; independent database snapshots include committed proposals, decisions,
state/evidence identity and audit details. The harness never writes lifecycle or
evidence rows. For the concurrent review case, an administrator temporarily holds
the existing operation row lock, observes two API-role sessions blocked on that
lock through `pg_blocking_pids`, then releases it. This selects and proves overlap;
it does not alter the protected decision function. Stop/restart the real reconciler
between displaying and deciding a binding where needed to avoid an unrelated
background lookup changing it. Record those supervisor actions explicitly.

Add API/MCP processes only to scenarios that need them, with slot-owned
loopback ports and the existing process-group/quiescence checks. Warm up
authentication before starting a scenario's observation window. Use exact
scenario-specific grants and the actual qualification contract version;
`rebuild` currently seeds review policies only for CRM v1, and demo grants
do not cover generated q-v2 resources. Reusing them unchanged would test a
denial rather than the intended review/agent interleaving.

Keep keys, cookies, CSRF values and authentication headers in private memory
or mode-600 harness credentials. Raw evidence should contain actor/key ids,
binding digests, tool names, outcomes, committed audit rows and monotonic
request intervals, never credential values. Observer/driver failures must
fail the harness, including errors raised in concurrent caller threads.

`chaos.arms.boundaries` implements the drivers; `chaos.boundary_coverage` joins
the independent sources. Scenario data has an explicit `review_mode`; notes
do not drive execution. Only F14 protected-retry cases register a q-v2 CRM
review policy. F15 admissions rendezvous after official MCP initialization,
and tool-call intervals exclude initialization. Its two credentials belong
to the same real service principal; cross-tenant checks use another tenant.
The runner retains admitted operation views if a driver fails, and missing
required boundary observations fail the oracle/CLI even with one correct effect.

Positive recovery uses temporary CRM `index_lag`, then normal `reindex`.
`drop_from_index` models permanent index loss and cannot be used to claim an
achievable reveal. No provider record, lifecycle transition or evidence row
is invented or edited. Actual lookup responses must match the independent
effect id and payload hash before committed success can qualify a recovery.

The F14 usefulness denominator includes its scripted, authorized human
participants. It does not claim automatic completion without a reviewer.
F15 completes through an ordinary provider lookup after all attacks are
refused, without a human approval. Neither driver measures model behavior.

## F14: actual stale and concurrent decisions

- Admit an authorized operation through the human API. Produce ambiguity
  through a real provider fault and wait for independently observed committed
  escalation. An investigator proposes REVERIFY; reviewers are distinct
  authenticated humans, never the requester/proposer.
- Concurrent variant: two reviewers approve the same displayed binding with
  distinct decision keys. Require actual overlapping requests, one accepted
  decision, one already-decided refusal, and exactly one committed decision
  row. A repeated click with the original key must replay that decision.
- Stale variant: after the proposal is read, reveal the actual provider effect
  and let the real reconciler record it. Observe the committed state/evidence
  change before submitting the old binding. Require a stale refusal with a
  committed audit record and no extra provider dispatch.
- Use a real effect and lookup to complete the positive path. Do not copy the
  integration test's made-up ATTEST_APPLIED reference into qualification:
  fabricated restoration evidence would fail the independent ledger oracle.
- A protected-retry variant needs an explicitly registered review policy for
  its q-v2 contract and definitive pre-execution rejection evidence. Do not
  accidentally let an unreviewed contract auto-retry before the human race.

Coverage must join the displayed binding, overlapping request intervals,
committed decision/refusal rows and the independent final effect count. A
scheduled race or two client response codes alone is insufficient. F14 is
applicable to SafeAct and B3. B3 uses its own conventional review queue and
version checks, without importing SafeAct's protected SQL decision functions.

## F15: actual credential rotation and attempted self-approval

- Use the official MCP client over the real MCP process. Create two scoped
  credentials for the same service principal. Switching the credential with
  the same intent must return the same operation; changing the intent on the
  same target must be refused under SAT28. This tests real credential rotation,
  not just a renamed intent or an invalid extra argument.
- Exercise concurrent submissions through those two credentials, a changed
  payload, forbidden caller-supplied identity/provider-key fields, revoked
  credentials and another tenant's visibility. Independently compare admitted
  rows and provider calls/effects before and after every attack.
- Propose a permitted resolution through MCP, then attempt unlisted approval
  tools and the separate human decision endpoint using only agent credentials.
  Require actual refusals and no committed human decision or approval-induced
  retry/evidence. Never allow the driver to approve its own request.
- Preserve the existing boundary: stable identity and the conflicting-target
  rule are scoped to a principal. Rotating credentials for that principal must
  not create a new identity; separately authorized principals are a different
  case and must not be used to imply a stronger global guarantee.

This is a deterministic adversarial driver, with no paid model call. It
qualifies boundary enforcement, not new model behavior. The published M4
model evaluation retains its own scope and tool/output-version disclosures.
F15 is applicable only to SafeAct; baseline cells are N/A, never passes.

F14/F15 source, evidence adapters and observed interleavings must be complete
before the M5.11 corpus/scoring freeze and STOP 2 review. Held-out combinations
remain excluded from development.

## Construction audit before freeze

F13 now spans payments and CRM. The existing generic `_combined` provider
retargeting is insufficient for F13+F12: a CRM-only `corrupt_search` switch
must not become an unused payments attribute, and a compensation lookup fault
must target the inverse step. Validate provider/switch compatibility and reject
unknown controls before execution. Check combination construction from the
declared contracts without using held-out outcome runs for development tuning.
This audit remains open; the standalone F13 evidence does not qualify a held-out
workflow combination.
