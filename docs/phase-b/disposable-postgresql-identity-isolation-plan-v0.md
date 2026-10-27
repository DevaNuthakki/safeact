# Disposable PostgreSQL Identity-Isolation Experiment — v0

Status: PROPOSED FOR REVIEW — NOT APPROVED

Project: SafeAct
Source checkpoint: 0fb22da
Depends on: PostgreSQL Execution-Role Isolation Audit v0
Scope: Architecture and negative-test design only

This proposal does not authorize database role creation,
permanent migrations, privilege grants, worker execution,
or external provider dispatch.

## 1. Verified starting evidence

The local read-only catalog audit established:

- The development connection uses safeact_dev.
- safeact_dev is a PostgreSQL superuser.
- safeact_admission_writer exists as a NOLOGIN role.
- safeact_execution_worker does not exist.
- Four permanent foundation tables exist in public.
- Those tables are owned by safeact_dev.
- RLS is disabled on the four foundation tables.
- Admission-writer access is restricted to selected tables.
- Runtime worker database isolation is not established.

The 386 regression tests previously passed:
243 Phase A and 143 PostgreSQL tests.

The development evaluation reported four PASS outcomes and
six NOT_RUN outcomes. Those results do not establish
production execution safety.

## 2. Threat model and trust boundary

The experiment must test an adversarial runtime principal
capable of issuing arbitrary SQL using its own credentials.

Assume the adversary may:

- Supply forged worker identifiers.
- Choose arbitrary operation and tenant identifiers.
- Change untrusted application parameters.
- Reuse old claim, attempt, or fence identifiers.
- Retry after a lost acknowledgment.
- Attempt direct table writes.
- Attempt role switching and privilege escalation.
- Invoke SQL functions directly.
- Manipulate caller-controlled session variables.

Do not assume a Python authorization result is
unforgeable inside the database.

The trust boundary is the authenticated PostgreSQL
session and independently protected authorization state.

## 3. Isolation environment

Preferred model:

Use a completely separate, disposable PostgreSQL cluster
or instance that has never held SafeAct development data.

Possible implementations include:

- A temporary local PostgreSQL cluster initialized for
  this experiment.
- A disposable containerized PostgreSQL instance.

Select based on verified local tooling and reproducibility.
Do not introduce a container orchestrator just for this.

The experiment must never connect to the existing
safeact_dev database to perform role DDL or privilege tests.

PostgreSQL roles are cluster-wide. A disposable schema in
the existing cluster is insufficient isolation for role
creation, deletion, or credential-security experiments.

Before executing any DDL, independently verify the
destination instance, its unique test database identity,
and its disposable lifecycle.

The administrative bootstrap credential must not be
supplied to an application runtime connection.

## 4. Proposed identity separation

Within the disposable instance, provision independent
authentication credentials for these logical principals:

Administrative bootstrap:
- Creates disposable objects and roles.
- May own the temporary cluster.
- Is never used to prove runtime privilege denial.

Object owner:
- NOLOGIN where supported by the design.
- Owns protected tables and privileged functions.
- Must not be inheritable by runtime login roles.

Admission runtime:
- Independent LOGIN credential.
- NOSUPERUSER and NOBYPASSRLS.
- No CREATEDB or CREATEROLE.
- Only the minimum admission-specific access.

Execution runtime:
- Independent LOGIN credential.
- NOSUPERUSER and NOBYPASSRLS.
- No CREATEDB or CREATEROLE.
- No administrative-owner membership.
- No unrestricted mutation of foundation tables.

Unauthorized runtime:
- Independent LOGIN credential.
- No admission or execution permission.
- Used for explicit negative controls.

Every test must establish the effective session principal
using a separately authenticated connection.

SET ROLE from an administrative session is not sufficient
evidence of independently isolated runtime credentials.

## 5. Direct SQL privilege-denial matrix

For each restricted runtime credential, test whether
unapproved SQL operations are rejected.

The negative matrix must cover:

- Unauthorized INSERT into protected tables.
- Unauthorized UPDATE and DELETE.
- Unauthorized TRUNCATE.
- Unauthorized ALTER TABLE and trigger disabling.
- Unauthorized CREATE in protected schemas.
- Unauthorized administrative role switching.
- Unauthorized role creation and privilege grants.
- Unauthorized access to execution-only functions.
- Unauthorized admission using execution credentials.
- Unauthorized execution using admission credentials.
- Unauthorized use of an unrelated runtime identity.

Protect schema USAGE and CREATE as separate privileges.

Check relevant ownership and inherited-role privileges,
including PUBLIC grants, not only explicit grants.

Test PostgreSQL default function EXECUTE privileges and
revoke broad access before permitting narrow entrypoints.

Check dangerous predefined role memberships and available
paths to administrative escalation.

A denied statement must also leave independently observed
database state unchanged.

## 6. Narrow execution entrypoint requirements

A future execution database entrypoint must not accept
a caller-selected worker subject as trusted authority.

If SECURITY DEFINER is used, independently review:

- Privileged function owner and its actual capabilities.
- Fixed, secure search_path.
- Fully qualified object references.
- Explicit EXECUTE permissions.
- PUBLIC privilege revocation.
- Input validation and authorization.
- Untrusted session-setting behavior.
- Direct SQL bypass prevention.
- Function replacement and object-shadowing attacks.

Inside a SECURITY DEFINER function, current_user can
represent the function owner rather than the login role.

The experiment must distinguish session_user,
current_user, and the authenticated application principal.

No positive provider-dispatch capability is introduced
at this stage.

## 7. Per-worker authorization is a separate problem

Distinct admission and execution database credentials
establish service-boundary isolation, not automatic
isolation between two workers sharing an execution role.

Proving per-worker isolation requires an additional
trusted identity and assignment mechanism.

Possible later strategies include:

- Separately authenticated worker-to-service requests
  followed by a trusted, revocable authorization lookup.
- Database-side verified authority where the database
  can independently validate the authenticated principal.
- A carefully designed service gateway whose DB rights
  cannot bypass its independently enforced decisions.

Do not choose between these alternatives without a
separate threat-model and bypass review.

The existing WorkerScopeReview is deny-only and not an
unforgeable database capability.

## 8. One-time execution and failure evidence

Once role separation passes, a later fake-provider
experiment must verify:

- The authenticated runtime role is authorized.
- The operation and assignment are still valid.
- Claim, attempt, fence, and invocation bindings match.
- At most one invocation-consumption event commits.
- A losing concurrent contender gets no call permission.
- Lost database acknowledgment does not authorize replay.
- A process crash does not create a second invocation.
- Direct SQL cannot mint an equivalent permission.

The previous experiment establishes a useful one-shot
pattern, but not exactly-once external provider effects.

Independent observation must separate database state,
invocation count, and simulated provider-effect count.

No real provider credentials or network calls are allowed.

## 9. Negative-test acceptance criteria

A future test suite may pass only when:

1. Test connections authenticate independently.
2. Runtime principals are not superusers or owners.
3. Runtime roles have no unintended inherited authority.
4. Forbidden SQL operations are actually rejected.
5. Denied operations leave protected state unchanged.
6. Role switching cannot confer another service identity.
7. Application checks cannot be bypassed by direct SQL.
8. Function EXECUTE and schema privileges are restricted.
9. Positive test-only admission behavior still works.
10. Existing SafeAct regressions remain green.
11. All temporary test infrastructure is cleaned up.
12. Final role and database inventories match baseline.

A partially executed test or insufficiently isolated
cluster is INCONCLUSIVE, not PASS.

Do not weaken checks or privileges merely to pass tests.

## 10. Controlled implementation gates

Gate A — review this proposed plan.

Gate B — inspect available isolated PostgreSQL tooling.
Do not provision anything during environment inspection.

Gate C — independently verify a disposable destination,
bootstrap boundaries, lifecycle, and cleanup method.

Gate D — provision distinct test-only credentials in
the isolated instance with guarded teardown.

Gate E — run SQL privilege-denial tests against distinct,
non-superuser login sessions.

Gate F — test admission and execution separation,
including malicious direct-SQL attempts.

Gate G — design trusted per-worker identity binding.

Gate H — only then consider a deny-only atomic SQL
invocation-consumption adapter with a fake provider.

Each gate must preserve independent regression evidence
and stop on unexplained differences.

## 11. Explicit unresolved findings

The shared runtime-credential subject-impersonation
limitation has not been repaired.

Canonical JSON enforcement remains unresolved.

Database-enforced individual worker identity binding
does not exist yet.

The execution-worker production SQL role does not exist.

SA-EVAL-003 remains NOT_RUN.

Final holdout evaluation has not been performed.

Production execution approval remains false.

This plan is not authority to deploy or dispatch.
