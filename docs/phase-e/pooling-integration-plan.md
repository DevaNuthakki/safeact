# Runtime connection pooling integration — 2026-10-24

The author authorized integration and regression/local timing checks. Pre-run,
frozen smoke, calibration and big-run execution remain explicitly prohibited
until the author changes that instruction. No staging, commits or pushes.

The exact pre-change tree matches the prior sealed 470-file snapshot and four
base-only references. Its full regression baseline is 1626 Python / 51 console
tests, typecheck/lint/build. Earlier local checks passed 78 targeted tests and
16 prototype pool safety checks. Earlier evidence packets remain immutable.

## Design

- Use the installed SQLAlchemy QueuePool to lease actual native psycopg
  connections. No dependency installation or weakening of strict connection
  checks. Retain Psycopg automatic preparation; do not force immediate prepare.
- Each owner has fixed credentials, an explicit bound and zero overflow, and
  belongs to its creating process. Refuse use after fork. Create worker owners
  lazily in prefork children; never share connections or queues between roles.
- Use explicit connection context factories for API transactions and worker,
  relay, recovery and advancement passes. Retain injected/fresh connection
  behavior and fresh observation/retry connections after uncertain commits.
- Returning a lease rolls back unfinished work and resets session settings,
  role, listeners, temporary objects and session advisory locks without
  deallocating prepared statements. Reset failures discard the connection.
  Checkout verifies liveness and role. Pool wait is bounded and sanitized.
- Preserve all protected SQL, authorization, state transition, evidence and
  claim/fence/outcome commit ordering. Resource cleanup never proves rollback
  of an uncertain business commit. No automatic replay of business work.
- API pools are lazy and close with application lifespan. SSE releases the
  connection after each poll. Background processes and threaded demo owners
  close their pools at shutdown; broken connections are replaced.

## Verification and completion

Add actual PostgreSQL checks for reuse, limits, isolation/reset, broken and
terminated connections, fork refusal, lost commit acknowledgements, duplicate
delivery and uncertain non-idempotent work. Validate both ordinary API requests
and deployed worker/loop wiring. Run focused checks, then full Python/UI
regressions and the same bounded local latency method through the integrated
connection ownership path. Preserve diagnostic originals and failures.

Archive old frozen input definitions before changes. Once implementation is
stable, refresh only the reviewed source candidate, explicitly verifying that
scenario, timing, contract, applicability and scoring definitions do not change.
This is data construction, never execution. Seal the updated integration
evidence and report any remaining limits. Stop without starting pre-run.
