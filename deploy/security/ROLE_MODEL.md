# ADR — SafeAct local PostgreSQL admission role

Status: Verified local-development design; NOT production authorization.

## Responsibilities

`safeact_dev` currently owns the development database and tables and
has SUPERUSER capabilities. It is an administrative/migration account,
not an application runtime identity.

`safeact_admission_writer` is a NOLOGIN permissions role with only:
- USAGE on the public schema
- SELECT and INSERT on safeact_operations
- SELECT and INSERT on safeact_audit_events
- SELECT and INSERT on safeact_outbox_events

It has no table privileges on safeact_attempts, no direct
audit-sequence permissions, and no schema/database CREATE rights.

Runtime logins must be separate, nonadministrative identities with
restricted membership. They must not receive the administrator password.

## Verified local evidence

Step 30C demonstrated transactional intent admission while SET ROLE
selected the restricted permissions role; eight forbidden SQL
operations were rejected.

Step 30D demonstrated independent password authentication with a
disposable login inheriting restricted privileges; ten forbidden
operations were rejected, including role escalation. Test records
were rolled back and the disposable login was deleted.

These manual checks are not substitutes for automated CI tests.

## Security boundaries and limitations

1. This role is exclusively for a trusted internal admission service.
   Never expose database credentials to end users, agents, browsers,
   external tool plugins, or untrusted execution environments.

2. It is NOT a tenant-isolation mechanism. Its table SELECT permissions
   can read records for every tenant. Its INSERT permissions also permit
   supplying arbitrary tenant_id and actor_id values.

3. PostgreSQL RLS is not enabled on these tables. A caller-controlled
   custom session variable would not by itself be a trusted tenant
   identity, because clients with arbitrary SQL access can change it.

4. A trusted authentication/authorization boundary must derive and
   verify tenant identity, actor identity, allowed action, provider
   account, and connector scope before constructing an IntentSubmission.
   Untrusted clients must not directly construct database writes.

5. Direct table INSERT privileges do not enforce the atomic operation,
   audit, and outbox bundle. The trusted application must use its
   verified transaction service. Stronger database-enforced admission
   requires a separate future design and threat-model review.

6. An UNKNOWN database-admission observation never proves noncommit.
   Neither CONFIRMED_RECORDED nor UNKNOWN is provider-side evidence
   that an external action happened or is safe to retry.

7. CONNECT and TEMPORARY may be available through PostgreSQL's PUBLIC
   role. Their effective availability must be evaluated separately
   from explicit grants. This local design does not claim they are
   revoked.

## Deployment blockers

- Authenticated and authorized tenant-scoped API boundary
- A production threat-model decision on cross-tenant database access
- Separate permanent runtime credential provisioning and rotation
- Owner/migration separation without a production superuser login
- Automated role-permission tests in deployment CI
- Verified external-effect dispatch/recovery authorization

No external provider dispatch is authorized by this role.

## Local provisioning

Run `provision_local_admission_role.sql` only as the trusted
administrator in the dedicated `safeact_dev` database, after applying
migration `0003_durable_lifecycle`. It also verifies that the admission
role holds no privilege on any 0003 lifecycle table, view, or function.

The SQL creates no LOGIN and accepts no password. It rejects unexpected
existing role attributes and validates effective privileges.

Provisioning is a database-administration action, not an Alembic
application-schema migration.

Do not reuse this development-only script as a production security
policy without adapting and reviewing its administrative assumptions.
