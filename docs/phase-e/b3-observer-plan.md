# B3 independent observation plan, 2026-10-23

Add a read-only adapter for B3's committed operations, audit, dispatch leases,
recovery deadlines and fixed workflows. Use a fresh repeatable-read, read-only
transaction per sample, with host monotonic intervals around all queries. Read
only B3 tables; never call its runtime, workflow inspection/locking methods,
SafeAct SQL, or the provider ledger to manufacture a local result. Missing
admitted identities or child jobs must fail observation. Select explicit fields;
never query human credentials or sessions. Database time determines lease and
deadline predicates; host wall time is not evidence of database expiry.

Keep B3's persisted workflow request and actual child resource/account values.
Do not synthesize a stored SafeAct plan. Teach the common workflow oracle to
validate this explicit request representation against the same scenario,
including child tenant/requester/request keys, contract, payload, account and
the separate inverse's exact original-charge reference. SafeAct's historical
representation and results must remain unchanged. Unknown representations
fail validation. B3's distinct CRM account is legitimate; its refund must still
use the original payments account. No arm-specific exemption from safety.

Verify with real disposable PostgreSQL and HTTP providers: commit visibility,
rollback, read-only enforcement, missing bindings, live/expired leases, actual
recovery deadlines, normal/rejected/lost/hidden workflow outcomes, and an
independent snapshot before an inverse effect. Run common-oracle counterexamples
for request/resource/account/reference/amount/identity corruption and replay
existing saved SafeAct evidence without changing its results. Run required
regressions and preserve failures and a new reviewable evidence packet.

This slice completes database normalization, not B3's process/fault-driver arm
or its F14 API driver. Do not register a partial arm in qualification, relabel
unsupported faults N/A, run held-out outcomes, freeze the corpus, commit, or
start final qualification. B0–B2 and the remaining cross-arm drivers follow.
