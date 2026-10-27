# Workflow replay binding correction

The customer-support API example exposed a counterexample to blueprint §03's
stable-identity requirement: submit a replacement, then replay the same key with
`fee_minor` changed from 500 to 501. Revision 0012 compared the stored order and
compensation plan and requester, but omitted the original charge's payload and
contract. It returned the old workflow with HTTP 200. No second charge was
admitted, but the changed intent was incorrectly accepted as an identical replay.

## Design before implementation

Add revision `0021_workflow_replay`; leave 0003–0020 rendered SQL unchanged.
Replace only `safeact_api_create_workflow` using the frozen 0012 definition plus
a check in its existing conflict branch. In addition to the stored plan and
requester, require the already admitted charge operation to match the tenant,
connector, action, customer, provider account, charge contract and canonical
payload. Missing or different charge identity raises the existing SAT22 conflict.

The workflow's unique `(tenant_id, request_key)` constraint remains the concurrency
arbiter. A losing insert waits for the winner's transaction, then compares the
winner's committed workflow and charge. The workflow and charge were admitted in
one transaction, so a partial new bundle is never exposed. Existing rows need no
backfill: compare directly with the immutable original charge. Neither plan nor
charge is rewritten. Grants are still checked before admission or replay.

No lifecycle states, locking protocol, permissions, signatures, tables, provider
calls or retry rules change. CREATE OR REPLACE preserves the function's owner
and grants. Downgrade restores the historical definition, including its replay
limitation; it is not a fix for this defect.

The internal experimental SQLAlchemy admission path had the same omission. Its
replay observer now compares the immutable original charge fields and canonical
payload directly, without replaying the provider call or rewriting admission.
This covers development-harness callers as well as the authenticated API.

## Required validation

- HTTP refusal for fee/customer/charge-contract/account changes under one key;
  the alternate values must be granted so a grant refusal cannot mask the test.
- Identical replay returns the same workflow and charge.
- Concurrent distinct-fee submissions produce one workflow, one charge and one
  conflict, with the winner's immutable amount preserved.
- Upgrade an existing 0020-era workflow; exact replay still works, changed fee
  conflicts, and function ownership/ACLs remain unchanged.
- API example over real HTTP and the independent provider ledger: clean order,
  verified refund of the original charge, and uncertain order without a refund.
- Old migration golden hashes, role isolation and the full regression suites.

This is a development correctness fix. Historical evidence packages remain
unchanged and describe their archived source; no frozen qualification run is
authorized by this change.

## Concurrent admission follow-up: revision 0022

A real API regression run logged `UniqueViolation` on
`uq_safeact_operations_provider_key` during identical concurrent submissions.
The insert handled only the tenant/request unique constraint, while native
provider keys have their own unique index. The loser can encounter that second
constraint first. It returned HTTP 500 even though the winner recorded one
operation and one effect. The old E2E test hid the thread exception because it
checked only the distinct successful IDs, not every thread's completion.

Design before implementation: keep both unique constraints. Revision
`0022_admission_race` replaces only the internal API admission function's insert
with untargeted `ON CONFLICT DO NOTHING`, covering both unique indexes. After
any conflict the existing code still reads by tenant/request key and compares
the immutable fields and requester; a conflict is never sufficient for success.
Different or missing records remain refused. The internal SQLAlchemy admission
insert adopts the same conflict handling; its existing observer verifies the
immutable identity and complete admission bundle. No exceptions are swallowed,
and no operation is updated on conflict. Owner, grants and signatures stay the
same; no state transitions, retries or provider behavior change.

Use futures in the duplicate HTTP E2E test so every worker exception reaches
the parent. Keep the exact request count, identity count and independent effect
assertions. Verify repeated barrier-synchronized batches through both API and
SQLAlchemy admission, plus changed-request conflicts, one audit/outbox bundle,
old golden hashes and the complete regression suites. The final regression run
must use the completed source with both replay corrections.
