# PostgreSQL Execution-Role Isolation Audit — v0

Status: PROPOSED FOR REVIEW — NOT APPROVED

Project: SafeAct
Source checkpoint: 1c3ad77
Evidence: Local read-only PostgreSQL catalog audit, Step 39D-15A-R1
Scope: Local development database only

This document preserves observed database facts separately from
proposed security controls. It is not a migration, deployment
approval, execution authorization, or production readiness claim.

## 1. Verified local database identity

- Database: safeact_dev
- Client host: 127.0.0.1
- Client port: 55432
- PostgreSQL backend port: 5432
- Session role: safeact_dev
- Current role: safeact_dev
- Transaction read-only: on during the audit
- Default transaction read-only: on during the audit

The differing client and backend ports are consistent with
forwarding or mapping. The audit did not establish the specific
network mechanism.

The audit performed catalog inspection without modifying
application records, roles, or permissions.

## 2. Verified PostgreSQL role inventory

### safeact_dev

Observed:
- LOGIN: true
- INHERIT: true
- SUPERUSER: true
- CREATEDB: true
- CREATEROLE: true
- BYPASSRLS: true
- REPLICATION: true

Decision:
This role is a development administration identity, not an
acceptable production execution-service credential.

A superuser connection must not be used to claim that
application-level worker authorization is database-enforced.

### safeact_admission_writer

Observed:
- LOGIN: false
- INHERIT: false
- SUPERUSER: false
- CREATEDB: false
- CREATEROLE: false
- BYPASSRLS: false
- REPLICATION: false

The role has limited table privileges, but the observed
configuration does not establish an independent authenticated
admission-service login.

No matching direct role memberships were observed.

### safeact_execution_worker

Observed:
- Role not present.

No execution-worker SQL privileges or database identity
binding can be claimed.

## 3. Verified schema and foundation tables

The following permanent tables exist in public:

- safeact_operations
- safeact_attempts
- safeact_audit_events
- safeact_outbox_events

All four:
- Are owned by safeact_dev.
- Have row-level security disabled.
- Do not have FORCE ROW LEVEL SECURITY enabled.

RLS is one possible isolation mechanism; its absence does not
alone determine whether a correctly restricted function-only
security design could work.

The local audit is not a production database audit.

## 4. Verified effective table privileges

safeact_admission_writer:

- Operations: SELECT and INSERT; no UPDATE or DELETE.
- Audit events: SELECT and INSERT; no UPDATE or DELETE.
- Outbox events: SELECT and INSERT; no UPDATE or DELETE.
- Attempts: no SELECT, INSERT, UPDATE, or DELETE.
- No TRUNCATE or TRIGGER privilege on these four tables.

safeact_dev:

- Has effective SELECT, INSERT, UPDATE, DELETE,
  TRUNCATE, and TRIGGER privileges on all four tables.
- Is also a superuser, which is a separate and more
  powerful administrative capability.

The observed admission role privileges are a useful starting
point, not proof of end-to-end admission identity isolation.

## 5. Existing mutation-guard function

Observed public.safeact_reject_mutation:

- Owned by safeact_dev.
- SECURITY DEFINER: false.
- Function configuration: null.

A mutation trigger can reject ordinary UPDATE or DELETE
attempts, but it does not isolate a superuser or table owner
capable of changing or disabling protections.

This audit did not independently retest trigger behavior.

## 6. Existing worker application boundary

The committed core.worker_authority module:

- Uses the existing RS256 identity verifier.
- Requires the dedicated worker audience.
- Reviews an immutable trusted assignment snapshot.
- Checks worker subject, operation, tenant, connector,
  and provider-account identity.
- Denies dispatch, retry, and database execution authority.

These checks do not prove that a SQL connection is confined
to the verified worker identity.

WorkerScopeReview is not an unforgeable capability and
must not be accepted by a database execution endpoint as
proof of permission.

## 7. Primary security finding

FINDING: DATABASE EXECUTION ISOLATION NOT IMPLEMENTED.

The local database superuser can exercise administrative
capabilities unavailable to a properly restricted runtime.

An application token verifier cannot repair this privilege
boundary merely by returning an accepted Python object.

A caller-controlled subject, worker label, session setting,
or connection parameter must not become trusted database
authorization evidence.

The known subject-impersonation limitation remains open.

## 8. Proposed separate runtime identities

Proposed future boundaries:

1. A migration/administration owner, not held by runtime
   services.
2. An independently authenticated admission-service
   credential with only admission-required permissions.
3. An independently authenticated execution-service
   credential with no unrestricted table-write capability.
4. A trusted and revocable worker-to-operation assignment
   source, not populated from caller-selected identity.
5. A narrow database authorization interface that denies
   direct SQL bypass and cannot mint broader authority.

This is a proposed architecture, not an approved role
definition or grant plan.

An execution role must not inherit the administrative
owner or superuser role.

## 9. Database entry-point design requirements

A future execution entry point must:

- Bind worker identity and operation scope to an
  independently trusted source.
- Recheck applicable operation, tenant, connector, provider
  account, claim, attempt, and dispatch-risk fence state.
- Enforce one-time invocation consumption in PostgreSQL.
- Preserve append-only audit evidence.
- Treat missing or ambiguous commit acknowledgment as
  a HOLD condition, not an invitation to retry.
- Return no reusable permission to call a provider.
- Prevent direct table writes from bypassing its checks.
- Deny access after assignment revocation where required.

SECURITY DEFINER is not automatically safe. If considered,
its owner, EXECUTE grants, search_path, schema qualification,
input validation, and caller privileges require independent
review. EXECUTE must not remain broadly granted to PUBLIC.

An untrusted custom session variable is not an identity
proof merely because it is read by a SQL function.

## 10. Required independent negative tests

Before execution privileges are considered, verify:

- Admission credentials cannot acquire execution authority.
- Execution credentials cannot acquire administrator access.
- A distinct worker cannot claim another worker's scope.
- A revoked or disabled assignment is denied.
- An unregistered worker is denied.
- Wrong tenant, connector, provider account, operation,
  claim, attempt, or fence is denied.
- Direct INSERT, UPDATE, DELETE, TRUNCATE, trigger changes,
  and privilege escalation are denied where applicable.
- Caller-selected SET ROLE or session identity assertions
  cannot bypass the intended authentication boundary.
- Concurrent contenders consume at most one invocation.
- Lost acknowledgments and replay never cause another call.
- All failed authorization cases have no provider effect.

A test using SET ROLE from a superuser session alone does
not prove separation between independently authenticated
production credentials.

Require real distinct, restricted database sessions for
the eventual credential-isolation acceptance gate.

No real provider call is part of the isolation experiment.

## 11. Controlled implementation sequence

Gate A:
Review and approve this findings document.

Gate B:
Design an isolated, disposable negative-testing fixture
with narrowly scoped, distinct PostgreSQL login principals.

Gate C:
Prove direct SQL privilege denial and separation of worker
and admission credentials, including forbidden role switching.

Gate D:
Design and verify an atomic, one-time execution-consumption
interface that remains deny-only for real providers.

Gate E:
Review trusted worker assignment and revocation enforcement.

Gate F:
Only consider a fake-provider development adapter after
the preceding controls independently pass.

No permanent production roles, migrations, or provider
dispatch are authorized by this document.

## 12. Evaluation and release status

Current development corpus:

- PASS: 4
- NOT_RUN: 6
- FAIL: 0
- INCONCLUSIVE: 0
- SA-EVAL-003: NOT_RUN
- final_holdout: false
- production_approved: false

Previously verified regressions:
- Phase A: 243
- PostgreSQL: 143
- Total distinct tests: 386

This document-writing step does not rerun those tests.

Canonical JSON enforcement remains unresolved.

The fake-provider concurrency regression fix is committed,
but its testing does not establish exactly-once external
effects or production-ready execution.

## 13. Open decisions

- Who owns database schema and SECURITY DEFINER functions?
- Which distinct login roles may connect to the database?
- Which role may call each narrow execution function?
- How is a verified worker principal securely bound to
  the database operation and assignment?
- How are worker permissions revoked without replay risks?
- Can direct SQL bypass be ruled out under the actual
  restricted runtime credentials?
- How are canonical payload bytes independently verified?
- What evidence is required before SA-EVAL-003 can run?
- How is a fault-injected lost-acknowledgment test observed
  independently of the implementation under evaluation?

Until answered and independently tested, the execution
boundary must remain deny-only.

No production execution approval is granted.
