# SafeAct — Persistent authority schema contract

Status: Proposed and verified in disposable PostgreSQL schemas.
No production migration has been applied.

## Authority records

The model consists of:

- safeact_auth_actors: tenant-scoped actors and enabled state.
- safeact_auth_identities: exact issuer/subject binding to one
  internal tenant and actor.
- safeact_auth_resources: tenant-owned resources with an exact
  connector and provider-account binding.
- safeact_auth_grants: exact actor, resource, connector,
  provider account, action, and contract-version permission.

All four tables default to disabled.

Foreign keys prevent grants referencing absent actors or
resources. A resource has only one configured provider binding
within a tenant under this initial contract.

Multiple identities binding to the same internal actor
are intentionally unsupported.

## Required transactional authorization

The next implementation must:

1. Verify the signed token before database authorization.
2. Enter an admission transaction using a restricted role.
3. Resolve identity and verify active actor membership.
4. Validate active resource ownership and exact grant.
5. Hold PostgreSQL row locks on the authority records.
6. Record the operation, audit event, and outbox event
   before releasing those locks.
7. Commit the entire transaction as one admission boundary.

Observation also requires a current authorization check
when exposed through an authenticated service.

## Privileges and concurrency

The admission role must not receive direct UPDATE or DELETE
rights on these tables, or uncontrolled SELECT access.

The earlier disposable experiment demonstrated a candidate
SECURITY DEFINER locking function. The permanent function
has not been implemented.

Its owner must be a dedicated restricted NOLOGIN role,
not a database superuser.

Administrator revocation and membership updates must
coordinate with the same locked rows. No application-side
cached allow result may bypass this check.

## Remaining work

- Review and create an Alembic migration.
- Provision separate administrator and function-owner roles.
- Implement a hardened authorization function.
- Integrate checks into submission and observation transactions.
- Test real concurrent revocation and resource reassignment.
- Verify database privilege boundaries independently.
- Plan versioning, auditing, and deployment of authority changes.
- Replace the existing static registry only when the persistent
  admission path is independently verified.

No production authorization storage, row-level security,
HTTP ingress, external provider dispatch, or unsafe automatic
retry is enabled by this schema contract.
