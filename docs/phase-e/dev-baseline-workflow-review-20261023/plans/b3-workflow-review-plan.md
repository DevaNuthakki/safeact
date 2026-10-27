# B3 workflow and review plan, 2026-10-23

Read blueprint §§04–05, 07–09 and the existing B3 design before this slice.
The prior execution packet remains sealed and historical. No frozen SafeAct
SQL, migration, product authorization or final qualification is changed.

## Fixed workflow

Extend only B3's disposable ordinary tables with immutable workflow requests,
step bindings and append-only advancement audit. Refactor B3 admission to accept
an existing transaction, keeping its public atomic admission behavior intact.
Commit workflow, first charge, step, intent audit and outbox together. Compare
every original field on replay, including requester, customer and fee.

An advancer serializes the workflow through its original charge job, then reads
step bindings and locks existing child jobs in fixed step order. Workflow and
step records stay insert-only; PostgreSQL row locking requires update rights,
so the coordination lock belongs on the already-mutable original job.
Only confirmed applied charge evidence can admit the order. A confirmed failed
order with authoritative proof for every attempt can admit a separate refund
of the actual charge reference and exact original amount/account. Uncertain
charge/order never permits an inverse. Child identities use the workflow id,
and all bindings and admissions share the advancement transaction. Refuse
missing/corrupt bindings rather than silently repairing them. Derived status
keeps incomplete or rejected compensation unresolved; verified compensation
requires the inverse's actual matching applied evidence. No provider call is
made by the advancer. Add a separately killable advancer loop.

## Human authentication and review

Use B3-owned human/password records, bcrypt and hash-only expiring bearer
sessions. Harness provisioning supplies humans and contract review policies
through the owned environment's administrator; the application cannot edit
principals/policies. Identity and tenant come from a live session, never request
fields. The local FastAPI boundary permits investigator proposals and distinct
reviewer decisions. It exposes no agent approval, attestation or caller proof.
Validation/error responses contain no credentials. This remains local baseline
infrastructure, not a second public product authentication system.

Proposals bind immutable intent, state/version, ordered attempts, exact observed
evidence, policy, action, proposer and expiry. Operation row locks serialize
decisions. The winning reviewer/key/decision replays after state progress; other
keys refuse. Changed evidence/version/policy or expiry produces a committed
stale refusal, with no authorization. Requester/proposer/reviewer independence,
role and tenant are checked inside the decision transaction. Concurrent reuse
of a reviewer's decision key for another proposal cannot authorize both.

Approved PROTECTED_RETRY creates a persisted one-use authorization for the next
attempt, bound to the same immutable intent/evidence/policy and expiry. Recheck
it at claim and fence, in addition to ordinary contract/budget/window checks;
consume it atomically with the attempt fence. A pre-fence abandoned claim does
not consume it. New evidence/attempts invalidate it. If ready work loses its
retry basis, return it to investigation instead of leaving it parked forever.
Approval is not provider non-application evidence.

Approved REVERIFY permits one extra lookup reservation, including outside the
normal interval/horizon; consume it atomically with that reservation. It cannot
authorize a dispatch or create provider evidence. Normal lookup results still
go through shared classification and completion checks. A lost investigation
after reservation may require a fresh human authorization beyond normal bounds.

## Verification and scope

Use actual PostgreSQL and HTTP mock providers, independent provider ledgers
and committed snapshots. Cover normal workflow, permanent order rejection and
refund, lost/hidden/rejected refund, uncertain order, concurrent advancement,
duplicate/lost-ack creation, wrong inverse bindings and transaction rollback.
Cover real HTTP login/role/tenant boundaries, overlapping reviewer decisions,
winner replay, new real provider evidence making a proposal stale, expiration,
policy changes, invalidated/consumed retry grants and one-use extra lookups.
Propagate every concurrent future exception. Keep failures and corrections.

Run complete regressions after implementation and retain a new source/evidence
packet without rewriting earlier sealed packets. Baseline normalization,
cross-arm drivers, B0–B2, construction/achievability audits and corpus freeze
remain required. A prominent FINAL BIG QUALIFICATION RUN announcement and the
author's explicit go after STOP 2 are still required before that run.
