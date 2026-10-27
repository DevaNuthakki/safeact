# SafeAct — Disposable atomic admission gate

Status: Tested SQL prototype only.

This is not an applied migration, deployed API, or
production identity-authorization boundary.

## Reproducible guarantees under test

- A dedicated NOLOGIN function owner controls bundle writes.
- The disposable runtime role cannot access authority
  or application tables directly.
- Exact identity/actor/resource/grant rows are locked
  in the same transaction as the operation, audit and
  no-dispatch outbox.
- Same-actor replay reuses the existing operation.
- Cross-actor request-key reuse and payload changes fail.
- Revocation waits for active authorization locks.
- Rolled-back or denied submissions leave no new records.
- No external action is ever dispatched.

## Deliberate deployment restriction

The SQL builder accepts only schema names matching
safeact_it_<32 lowercase hexadecimal characters>.

It cannot target public or another permanent schema.

## Security limitations

Issuer and subject are still function arguments.
The function does not verify the JWT signature itself.
A caller possessing its EXECUTE credential could assert
another subject.

Tests using SET ROLE through local development superuser
access do not constitute a production credential boundary.

The function relies on application-side canonical payload
preparation, although PostgreSQL checks JSON object structure
and the byte-level SHA-256 binding.

No production database migration, identity-provider
integration, credential isolation, HTTP ingress, authorization
administration API, or revocation event audit has been created.

## Future acceptance requirements

Before migration or production use:

1. Review identity provenance and runtime credential isolation.
2. Define admin-only membership and revocation transactions.
3. Review the whole SQL function for malicious input,
   conflicting data, privilege escalation, and concurrent races.
4. Verify that production credentials cannot bypass the gate
   or invoke it using arbitrary asserted identities.
5. Integrate real signed-token verification with the
   persistent authority admission path.
6. Preserve uncertainty after ambiguous database commits.
7. Require independent security review of migration,
   privileges, ownership, and RLS decisions.

## Adversarial tests and known vulnerabilities

The automated test suite deliberately includes negative
controls demonstrating gaps that remain unresolved.

### Identity assertion vulnerability

The same restricted runtime database credential can invoke
the SQL gate using either `subject-a` or `subject-b` when both
identities have otherwise valid grants.

This confirms that the SQL gate does not establish identity
provenance. It must not be exposed directly to untrusted
callers or treated as a complete authentication boundary.

A successful negative-control test indicates that the known
vulnerability was reproduced, not that it was fixed.

### Payload canonicalization limitation

A noncanonical JSON string with a matching SHA-256 digest
can pass the database constraints and be recorded.

The existing Python canonicalization layer is stronger,
but the SQL prototype cannot currently replace it as the
complete validation boundary.

### Additional fail-closed regression coverage

- Disabled actor records deny new admission.
- Disabled identity bindings deny new admission.
- Disabled exact grants deny new admission.
- Missing original no-dispatch outbox evidence denies replay.
- The existing legacy admission role cannot execute this
  disposable prototype gate.

## Mandatory conditions before production use

Do not apply this prototype to the permanent database
or expose its EXECUTE privilege to an untrusted client.

Identity provenance and canonical payload enforcement
require independently reviewed solutions before migration.
