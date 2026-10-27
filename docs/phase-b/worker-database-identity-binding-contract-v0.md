# Worker-to-Database Identity Binding — v0

Status: PROPOSED FOR REVIEW — NOT APPROVED

Project: SafeAct
Source checkpoint: 9b1d3b5
Scope: Disposable PostgreSQL identity-binding proof only

This is a design and test contract, not permission to
implement production dispatch or deploy database roles.

## 1. Verified existing boundaries

The current IdentityVerifier authenticates a JWT using
trusted RS256 public keys and validates identity claims.

WorkerScopeReviewService checks an authenticated subject
against an immutable server-provided assignment snapshot.

WorkerScopeReview is an ordinary forgeable Python object.
Its execution, dispatch, and retry authority properties
all return false.

VerifiedDisposableAdmission checks token validity before
using a PostgreSQL connection. It checks current_user,
but its SQL admission gate still receives issuer and
subject as function arguments.

A compromised runtime that can invoke the admission SQL
with its database credential may assert another enabled
issuer and subject. Python-side verification alone does
not prevent this database-level impersonation.

Existing PostgreSQL integration tests using SET LOCAL ROLE
do not prove independent login authentication.

The isolated PostgreSQL security suite now establishes
service-level permission separation with three
independently authenticated restricted login credentials.

## 2. Security objective

Demonstrate that a restricted worker authenticated as
worker A cannot convince PostgreSQL that it is worker B.

The trust boundary must not depend on:

- An arbitrary worker_subject argument.
- A forged VerifiedPrincipal Python dataclass.
- A forged WorkerScopeReview Python dataclass.
- A custom session variable set by an SQL caller.
- A caller-controlled tenant or operation identifier.
- An unrestricted shared runtime database credential.
- SET ROLE performed by a privileged test administrator.

The proof must use genuinely separate login connections.

## 3. Selected test-only mechanism

Provision two independent PostgreSQL worker LOGIN roles
inside a separate disposable PostgreSQL cluster.

The administrative bootstrap role is confined to fixture
setup, state observation, and guarded teardown.

A NOLOGIN object-owner role owns protected authority
records and authorization functions.

Each worker role must be:

- LOGIN, using its own SCRAM credential.
- NOSUPERUSER.
- NOCREATEDB.
- NOCREATEROLE.
- NOINHERIT.
- NOREPLICATION.
- NOBYPASSRLS.
- Unable to assume the owner or another worker role.

An independent admission role must remain unable to
acquire worker execution-function privileges.

This is an experimental credential model, not yet an
approved approach to production worker scaling.

## 4. Protected worker assignment model

The disposable test database contains a protected
worker assignment relation.

Each fixture assignment includes:

- Authenticated database login role.
- Operation UUID.
- Tenant identifier.
- Connector identifier.
- Provider account identifier.
- Enabled or revoked state.

The object owner controls assignments.

Worker roles may not insert, update, delete, truncate,
or change ownership of assignment records.

Worker roles must not read assignments directly.

Workers may only call the explicitly granted,
narrow-scope test authorization function.

## 5. Database identity provenance

The SQL authorization function must derive the calling
worker identity from PostgreSQL session_user.

The function must not derive authority from a supplied
worker name or arbitrary custom session setting.

Within SECURITY DEFINER execution, current_user may
represent the privileged function owner. It must not be
mistaken for the authenticated worker.

Tests must verify that a restricted worker cannot change
session_user through SET SESSION AUTHORIZATION.

Worker-to-role bindings must be controlled by trusted
administrative setup, not arbitrary worker SQL.

The admission service and unauthorized principals must
not acquire worker function EXECUTE privilege.

## 6. Deny-only scope review interface

The proposed disposable function evaluates an exact
worker-and-operation scope match.

Its inputs may contain an untrusted operation UUID,
tenant, connector, and provider account.

Its identity comes from the authenticated database
session, not from any request parameter.

A review may return a matching-scope diagnostic.

Provider dispatch, retry, and durable execution
authorization must remain false regardless of whether
scope matches.

No function may return a reusable provider-invocation
credential, dispatch token, or permission to bypass the
existing one-time invocation fence.

No real provider adapter may be called.

## 7. Revocation and concurrency requirements

An enabled assignment may be revoked only by an
appropriately privileged administrative test connection.

The scope-review query must observe protected assignment
state and must not rely on an indefinite cached snapshot.

A review after revocation has committed must be denied.

A review concurrent with revocation needs an explicit
lock and transaction-ordering test before any claim of
linearizable revocation behavior is made.

SELECT FOR SHARE against a protected assignment row is
one candidate for this experiment.

A function using row locks must not incorrectly claim
to be STABLE or IMMUTABLE.

The acceptance test must not infer real provider safety
from a scope lookup alone.

## 8. Direct SQL bypass resistance

Tests must deny:

- Worker A inspecting worker B's operation scope.
- Worker B inspecting worker A's operation scope.
- A worker using the wrong tenant.
- A worker using the wrong connector.
- A worker using the wrong provider account.
- A worker using an unknown operation.
- A disabled or revoked worker assignment.
- An admission service calling the worker function.
- An unrelated principal calling the worker function.
- Direct reads or writes of protected assignment records.
- SET ROLE to owner or another worker.
- SET SESSION AUTHORIZATION to another worker.
- Unauthorized grants, role creation, or function changes.

The SQL function must have a fixed search_path and
qualified protected object references.

PUBLIC EXECUTE must be revoked explicitly.

Function ownership and every effective privilege must
be checked independently from the application code.

## 9. Positive and negative controls

The positive control is a scope match for the correct
authenticated worker, operation, and metadata.

That match must be observable as review evidence only.

The negative controls must prove that an independently
authenticated different worker cannot reproduce that
scope match with its own credentials.

A forged custom session variable must not alter the
authenticated role binding.

After denied SQL attempts, an independent administrative
connection must verify that protected records, role
memberships, and function privileges are unchanged.

No acceptance gate may count a skipped test as PASS.

## 10. Disposable environment and cleanup

Use a newly initialized PostgreSQL 14.23 instance outside
the SafeAct repository and the existing safeact_dev cluster.

Bind to the loopback interface only.

Require SCRAM-SHA-256 authentication.

Use a random temporary port, separately authenticated
connections, private filesystem permissions, and an
invocation-specific cleanup marker.

Do not modify roles in the existing development cluster.

Always verify the exact PostgreSQL data directory before
administering disposable test fixtures.

Shut down the server before removing its temporary files.

If shutdown or cleanup identity is uncertain, preserve
the directory and report the failure.

## 11. Integration and regression requirements

Keep existing application execution behavior deny-only.

Do not change IdentityVerifier, WorkerScopeReviewService,
VerifiedDisposableAdmission, or atomic admission SQL
merely to satisfy a synthetic test.

Keep the committed 243 Phase A tests intact.

Keep the committed 143 PostgreSQL integration tests intact.

Keep the committed six isolated credential-security tests
intact.

Introduce new tests as an additional independent gate.

Compare a complete regression run against the existing
392 distinct tests, not against an invented result.

Test-only evidence must not be represented as production
worker credential provisioning.

## 12. Explicit remaining limitations

An authenticated per-worker PostgreSQL session proves
database identity isolation within the tested mechanism.

It does not automatically bind a verified JWT subject
to that session or establish the security of a production
worker gateway.

Production assignment provenance, credential lifecycle,
revocation propagation, and worker scaling need separate
decisions and fault tests.

A shared privileged application credential still presents
an impersonation risk if untrusted callers can execute
arbitrary SQL through it.

The admission SQL canonical JSON enforcement issue is
not resolved by per-worker credential testing.

The one-time fake-provider experiment does not prove
exactly-once external effects.

SA-EVAL-003 remains NOT_RUN.

Final holdout evaluation remains incomplete.

Production execution approval remains false.

No real provider dispatch is authorized.

## 13. Next acceptance gate

Build an isolated PostgreSQL negative-test suite which:

1. Authenticates two independent worker login sessions.
2. Proves correct-worker scope review matches.
3. Proves cross-worker and cross-scope review denial.
4. Proves caller-selected identity cannot replace
   session_user.
5. Proves a committed assignment revocation is effective.
6. Denies direct assignment-table SQL mutations.
7. Denies unauthorized function execution and role changes.
8. Independently verifies final protected database state.
9. Shuts down and removes its disposable cluster.
10. Preserves all existing SafeAct regressions.

Any unexplained failure is a failed or inconclusive gate,
never an authorization to relax privileges.

This contract is not production execution approval.
