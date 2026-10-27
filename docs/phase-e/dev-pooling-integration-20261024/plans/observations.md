# Milestone 5: defects found and items under observation

Each entry records what was seen, the evidence, and what was done. None is removed when fixed. The milestone exit evidence links here.

## 2026-10-20: prefork Celery worker forced to `fork` on macOS (M5.2), under observation

**Seen:**
- The first separate-process run of the stack (`test_stack_processes`) left every operation RECORDED.
- Every task failed in the prefork children:
  - first with `ValueError: not enough values to unpack (expected 3, got 0)` in `celery.app.trace.fast_trace_task`;
  - then, with `FORKED_BY_MULTIPROCESSING=1` tried, with `NotRegistered: 'safeact.execute_operation'`.

**Why spawn failed:**
- billiard (Celery's pool) defaults to the `spawn` start method on macOS (`billiard.context`, `darwin` branch) and to `fork` on Linux.
- A spawned child is a fresh interpreter: it rebuilds the Celery app from its pickled settings and does not import `deploy.stack.worker`. So it has neither the task registered by `make_app` nor the `app.safeact_execute` callable set by that module, nor the fast tracer state the parent prepared in `setup_worker_optimizations`.
- On Linux the forked child inherits all three.

**What was done:**
- `deploy/stack/worker.py` calls `billiard.set_start_method("fork", force=True)` before the pool starts, so macOS matches Linux.
- The execution worker and its HTTP clients are built lazily per process, after the fork. No connection pool or socket is shared across the fork.

**Risk this introduces (stated, not hidden):**
- Forking a process that has already used certain macOS system frameworks can crash the child. Examples are Objective-C runtime classes and some CoreFoundation or networking APIs initialised in the parent: the "may have been in progress in another thread when fork() was called" abort.
- The worker parent here loads only Python, psycopg (libpq) and Redis client code before forking, and creates no HTTP clients before the fork. It has not crashed in any run so far.
- That is an observation, not a guarantee.

**Rule for qualification (from the STOP 1 review):**
- If any run, the full qualification run above all, shows a worker crash, check fork safety first, before attributing it to SafeAct:
  - the crash signal and the macOS crash report;
  - whether it reproduces in a Linux worker.
- Prefer running the qualification workers in Linux containers (the Compose images, M5.14) when that avoids the issue. The run's environment record states where the workers ran.

## 2026-10-21: intermittent agent-harness failure (fixed in M5.3)

**Seen:**
- `tests/e2e_agent` failed in 2 of 3 consecutive local runs (macOS, dev compose PostgreSQL and Redis). Each failing run had 2 errors and took 588 s (a passing run takes 165 s). One run passed in between.
- Both failures showed the same sequence:
  1. The first test's `Stack.start()` failed while building the embedded MCP server. `uvicorn.Config()` called `logging.config.dictConfig`, which flushed every registered handler and hit `AttributeError: 'StreamHandler' object has no attribute 'stream'`.
  2. The failed start left its in-process Celery worker running.
  3. The second test started another worker in the same process. Kombu raised `RuntimeError: concurrent poll() invocation` and the worker died, so no task ran and the harness reported "prior attempt did not escalate in time".

**Cause (from the traceback):**
- `logging.Handler.__init__` adds a handler to `logging._handlerList` before `StreamHandler.__init__` sets `self.stream`.
- uvicorn's `dictConfig` ran in the main thread while the Celery worker thread was constructing its log handlers, and flushed one that was half built.
- The race is timing-dependent. It appeared during M5.3, but nothing M5.3 changed in this path (the only shared change is a redaction regex). It is treated as a latent defect that became visible, not as a regression.

**Fix:**
- Every embedded uvicorn server is now built with `log_config=None`, so uvicorn never runs `dictConfig`. This covers `deploy/demo/run_stack.Stack` (API and MCP) and `connectors/http_mock/servers.MockProviderServers`. It also stops uvicorn from replacing the process's JSON logging configuration.
- `Stack.start()` now stops whatever it already started if start-up fails, and `stop()` skips threads that never started. A failed start can no longer leave a worker sharing the next stack's event loop.

**After the fix:**
- 3 of 3 consecutive runs passed (177.6 s, 165.4 s, 165.2 s).
- The full suite was rerun.
- Logs of the failing runs are kept locally (not committed). They contain no secret: the agent harness uses scripted models only.

## 2026-10-21: disclosure, the MCP status output gained `next_check` (M5.6)

**What changed:** `get_action_status` (guarded MCP tools) now returns a `next_check` field with only the kind and two timestamps, never prose. The tool definitions are unchanged, so the pinned toolset digest (`safeact-guarded-v2`) and the frozen M4 agent manifests still match.

**Consequence:** a model calling the tools now sees one more authoritative field than it did in the M4 runs. Any rerun of the M4 agent evaluation on this code is therefore not identical to the published runs, and must say so. The published M4 results describe the code at the M4 exit commit.

## 2026-10-22: API reconnect journey assumed a change within one stream

**Seen:** the API E2E suite passed seven journeys and failed
`test_connection_interruption_catches_up_from_last_event_id` because its
cursor remained `None`. The first stream reached its four-second lifetime
without a `changes` event. Integration, browser, agent and development
recovery checks were running concurrently against separate schemas in the
same PostgreSQL database.

**Test assumption:** the change-feed horizon is the database snapshot xmin,
so an open transaction in another schema can delay the feed. A later
diagnostic sample observed an active administrator transaction aged 4.6 s.
That supports the timing explanation but does not reconstruct the exact
transaction present during the failed stream. Ending with `reconnect`
before a change becomes visible is allowed by the API contract.

**Change:** the journey now uses its existing bounded `follow` helper to
honor reconnect events until the first submitted operation arrives. It
then drops that stream and still verifies catch-up for work performed
while disconnected. No API or database behavior changed. A Phase A
counterexample supplies a reconnect before the first change and checks
cursor propagation and closure of both streams.

The original failed run is retained at `/tmp/safeact-api-e2e-current.log`;
the post-change suite is recorded separately at
`/tmp/safeact-api-e2e-current-rerun.log`. These paths are local development
artifacts, not committed qualification evidence.

**Post-change checks:** all 8 API journeys passed in 43.3 s; all 10
Playwright journeys and both scripted-agent tests also passed. The new
reconnect counterexample passed with the other SSE unit tests.
Final checks covered 1,293 distinct Python tests and 35 console tests, plus
typecheck/lint/build. PostgreSQL/broker discovery passed 535 tests with two
isolated-Redis opt-in skips; both skipped tests then passed separately with
their flag enabled. The documented local integration command now includes
that flag.

## 2026-10-22: concurrent-login regression hit the existing statement timeout

The first F13 full integration run logged `QueryCanceled` in one of the eight
threads of `ThrottleTests.test_concurrent_guesses_are_all_counted`, inside
`safeact_password_matches`; the parent test then failed its accounting
assertion. API connections use the existing five-second statement timeout.
The run overlapped browser/agent suites, isolated security checks and mock
development scenarios on the local machine. This makes resource contention
a suspected influence, not a demonstrated cause; per-test CPU/lock samples
were not captured at the failed statement.

The same test passed on an independent rerun (5.2 s including fixture setup)
with the same timeout and all assertions unchanged. Authentication SQL, cost,
locking and application timeouts were not changed. The original full-run log
is `/tmp/safeact-f13-integration.log`; the separate targeted rerun is
`/tmp/safeact-f13-login-rerun.log`. Both outcomes are retained while the full
regression check is completed.

The complete follow-up database run passed all 538 tests with no skips in
687.2 s, after the other suites finished. The first run passed 537 and failed
one in 837.0 s. Both logs and the unchanged targeted rerun are retained in
[the F13 regression evidence](dev-workflows-20261022/REVIEW.md). This resolves
the regression gate for this slice; it does not establish the cause of the
original timeout. Final total: 1,313 Python and 35 console tests passed,
plus typecheck/lint/build. Authentication timeouts were not relaxed.

## 2026-10-22: workflow replay omitted the original charge identity

The customer-support API example's new real HTTP check submitted a workflow,
then replayed its key with fee 501 instead of 500. All three workflow outcomes
returned HTTP 200 instead of 409. The original charge was preserved and no new
effect was admitted, but the caller's different intent was treated as an exact
replay. The order-SKU change already received 409, so the old SKU-only replay
test did not expose this omission. The internal SQLAlchemy workflow path had
the same gap.

Revision 0021 replaces only the API workflow admission function's replay check
and compares the immutable original charge fields and canonical payload. The
internal path now performs the equivalent read-only comparison. Existing rows
require no backfill; 0003–0020 rendered SQL and golden hashes stay unchanged.
The [design and validation plan](workflow-replay-binding.md) covers granted
alternatives, concurrent distinct-fee submissions, exact replay after upgrade
and preserved function ownership/ACLs. Focused API database checks passed all
six tests. The fix was then applied to the local development DB, followed by
the existing admission-role provisioning script.

The first example test attempt omitted CSRF by bypassing its helper's POST
method and received 403; it was corrected to use the example's real sign-in.
The resulting 200-vs-409 failures are the reproduction above. Both original
outcomes remain in the local development logs; the checks were not weakened.

## 2026-10-22: duplicate admission raced on a second unique index

The API E2E development log reported nine tests OK but also contained an
uncaught exception in a duplicate-submission thread. One request returned HTTP
500 after `UniqueViolation` on `uq_safeact_operations_provider_key`. The
successful requests still identified one operation with one effect; the old
parent assertion only checked the set of returned IDs and missed that a thread
had failed. This was a defective test pass, not a clean regression result.

The API admission insert targeted only the tenant/request constraint, while a
derived native provider key has another unique index. New revision 0022 handles
conflicts on either index, then retains the existing immutable identity and
requester comparison before returning a replay. Internal `record_intent` uses
equivalent insert handling and its existing complete-bundle observer. Neither
constraint was removed or weakened. Both API and internal paths still refuse
changed payloads. The E2E test now collects futures, so every thread exception
fails the parent, and it asserts all four replies.

Three focused integration tests passed: four batches of six synchronized API
callers, four batches of six internal callers, and competing internal payloads.
Each batch checks one identity and its complete audit/outbox bundle; competing
payloads produce one winner and one declared conflict. The final API E2E run
passed all nine journeys with no thread exception or server-error log. The
[customer-support verification packet](../demo/validation-20261022/REVIEW.md)
retains the original defective pass and the later stricter checks separately.

## 2026-10-22: resource choices depended on permission-list positions

Review of the new workflow form found array-position values for customer and
case selections. A refreshed permission list inserting an earlier choice could
redirect that position to a different granted target. The existing single-action
form had the same pattern and also interpreted an empty selection as index zero
through `Number("")`, allowing its first resource without an explicit choice.

Both forms now retain a stable identity containing the exact connector, account,
action, resource and contract. Missing or revoked choices fail validation rather
than switching targets. Unit checks wait for the inserted choices to appear and
assert that the submitted customer, case and charge remain the original choices.
Another check enters a valid amount without choosing a charge and asserts that
no action is submitted. API grants and admission authorization are unchanged.
## Review/agent driver development, 2026-10-23

The F14 concurrency observer first checked only sessions blocked directly by
the harness's row-lock holder. PostgreSQL can put the second tuple-lock waiter
behind the first waiter. Both API requests consequently reached their existing
five-second timeout while the observer waited for an impossible direct pair.
The harness now observes the actual transitive `pg_blocking_pids` graph, then
releases the lock before the unchanged timeout. Both request intervals and the
committed decision/replay are required for coverage. The original diagnostic
run remains in dev-boundaries-20261022/diagnostics/direct-blocker-observer/.

Initial positive-recovery fixtures used CRM `drop_from_index`, which represents
permanent index loss. `reindex` deliberately preserves those lost records, so
the system correctly stayed unresolved. Fixtures now use temporary `index_lag`
for a planned positive reveal. The permanent-index run remains in diagnostics/;
its driver exceptions also exposed missing timed views in the runner's exception
path. The runner now reads admitted operation views before closing that window,
keeping a driver failure distinct from an actually lost operation. Intermediate
failure summaries are retained, not relabeled as successful SUT qualifications.

The final nine-case development run passed with independently replayed raw
results, observed boundary coverage and complete quiescence. It is not the
frozen corpus or a load benchmark. No real Stripe or model call was made.

The following full database regression ran 547 tests in 1616.941 seconds and
failed one existing outage polling-count assertion: 10 passes versus >30. The
unchanged isolated test passed in 5.192 seconds. Longer host scheduling delays
are a possible explanation, not an established cause. Preserve both logs and
the quiet full rerun; do not loosen timeouts or infer a production safety failure
from that polling-count assertion alone.

The quiet full rerun passed all 547 tests in 702.147 seconds with no skips.
Separate new B3 admission tests passed all nine checks; all 795 Phase A tests,
21 isolated-security, nine API, two scripted-agent and 51 console tests passed.
No timing assertion or production policy was changed to obtain those results.

## B3 execution and lookup completion, 2026-10-23

Building the independent B3 runtime reproduced a real SafeAct evidence bug.
A payment applied once and lost its reply; a lookup began within the native
key window but the provider handled it after actual cache expiry. SafeAct used
the pre-request time and recorded `proven_not_applied` for the applied operation.
The new completion guard checks fresh database time after the HTTP answer and
downgrades late negative answers to non-decisive evidence. Matching positive
receipts remain usable. No frozen SQL, lifecycle pair or grant changed. The
[binding plan](lookup-completion-binding.md) and new evidence packet retain the
original failing test and the passing test of the same real timing boundary.

The first B3 lost-fence-ack test also exposed an observer error: its 100 ms
lease expired before fencing, yet a hook fired after a read-only refusal. Commit
hooks now require an actual mutation and assigned transaction identity. The test
uses a lease that reaches the fence and verifies the actual persisted attempt;
its original failed log is retained. Other tests still exercise expired leases.

Full Phase A discovery found that Celery's shared task registration made the
new baseline task visible in SafeAct's task registry. Both factories now declare
private tasks. Tests check both factory creation orders and keep the existing
assertion that SafeAct registers only its execution task. The failed discovery
and successful rerun are retained; no registry assertion was weakened.

The reconciler's bounded scan now selects due work before applying its limit.
Otherwise old retired rows could consume every slot and starve a newer unknown
operation. A separate test places 101 retired scheduling rows before due work;
those rows do not stand in for provider evidence or qualification outcomes.

## B3 workflow and human review, 2026-10-23

The first workflow implementation tried to row-lock an insert-only workflow
record. PostgreSQL requires UPDATE privileges for that lock, so the ordinary
application role correctly refused it. Advancement now coordinates on the
original mutable charge job and rereads step bindings after acquiring that
lock. Waiting concurrent advancers therefore see the winning committed step.
Workflow and step records retain their insert-only grants. The initial failed
suite also found a test listener attached to the independent observer engine
instead of the advancer's engine; the rollback check now intercepts the actual
writer. The following iteration made the charge-versus-inverse join explicit
because the step table has two foreign keys to jobs.

An initial human-authentication import assumed a Python bcrypt package that
is not in this repository's pinned environment. The implementation instead
uses the existing pgcrypto bcrypt-12 dependency through ordinary bound SQL,
without importing SafeAct's protected identity functions. Baseline application
roles and harness provisioning suppress statement, duration, sampled and
parameter logging before handling credentials. Actual HTTP tests verify
hash-only sessions, live tenant/role checks, reviewer independence, revocation,
validation redaction and the configured logging settings. No dependency or
product authentication setting changed.

Review introduced an important recovery case: an actual lookup can finish
after approval has made a retry ready. Its late observation must wake ordinary
reconciliation even while the broker is down. B3 now marks late observations as
pending and includes ready jobs with new evidence in its due-work scan. The
actual positive answer resolves that job without publishing or dispatching a
second attempt. Approval remains bound to the exact evidence and next ordinal;
changed evidence invalidates it at the dispatch fence.

The first added Retry-After approval test used CRM's 429 as a definitive
pre-execution rejection. That contradicts the actual CRM connector contract:
the answer is ambiguous, and review correctly refused a protected retry.
The corrected delay test uses payments' documented pre-execution 429. A separate
CRM regression preserves the refusal and verifies one dispatch, no effect and
no authorization. The original failed 75-test run remains diagnostic evidence;
no contract, assertion or timing bound was weakened to turn ambiguity into proof.

The selected development check passed twelve real HTTP/DB workflow and review
cases with eighteen effects (fifteen forward, three separate inverses), twenty-
seven dispatches and twenty lookups. Read-only replay matches their committed
snapshots and independent provider ledger; fifteen synthetic mutations of that
saved data are rejected separately and are not provider executions or coverage.
The replay's first cleanup assertion expected API exit zero. This pinned
Uvicorn version deliberately re-raises SIGTERM after draining, so the fixture's
owned-process shutdown exits `-15`. The original verifier and failure remain;
the corrected checker requires that exact signal exit for API cases.

## B3 independent database adapter, 2026-10-23

The workflow oracle previously assumed every step shared the charge's account
and used SafeAct's customer-based inverse resource. B3's stored request allows
distinct payments and CRM accounts and uses the actual original charge as the
refund resource. Rewriting those persisted values in an observer would hide
the real binding. The adapter instead emits B3's native immutable request and
unchanged children. The common oracle explicitly validates that representation
against the authorized business request and the actual provider charge, while
preserving the historical SafeAct plan path. Seventeen earlier SafeAct rows
replay with unchanged results. No frozen qualification scoring was changed;
the corpus/scoring review and freeze remain open.

Real PostgreSQL tests establish that uncommitted updates/rollback are invisible,
a writer can commit during a repeatable-read sample without mixing job/audit
versions, and PostgreSQL enforces read-only observation. Dispatch leases and
investigation deadlines are compared using actual database time; crossing the
deadline does not manufacture proof or change state. Observation reads no
humans, sessions or provider truth and does not acquire runtime coordination
locks. Five retained workflow cases pass common-oracle safety checks; hidden
and rejected inverses remain unresolved. Twenty-seven mutations of the saved
records fail separately and do not count as provider executions or coverage.

The replay command first omitted the repository import path, then exposed a
tuple-versus-JSON-array comparison in the verifier. The final comparison
normalizes only JSON types, preserving every oracle value and expected result.
Both diagnostics remain in the new packet. Historical artifact manifests have
both list and object layouts; the archive checker now reads each declared
layout when verifying the six previous packets, without rewriting them.

## B3 process observations, 2026-10-23

Actual HTTP, commit and Celery observations now come from B3's own processes.
The independent provider ledger and database samples remain separate sources;
stage selection alone cannot establish a send, committed visibility or lost
acknowledgement. B3's actual `LostAcknowledgement` exception is retained, and
the common coverage check accepts it with the same xid/stage/process/order
joins required for the original `ConnectionError` path. Workflow observations
carry native workflow identity. No SafeAct trace schema guard was loosened.

The first new mutation test supplied `error_type` twice; its fixture is fixed.
Full Phase A then found an optional tracing-field access on a minimal Celery
factory configuration. Disabled tracing now checks the requested role before
accessing trace configuration; the unchanged factory-isolation test passes.

Full discovery also exposed an intermittent concurrent file-creation failure:
`openat` with nonexclusive creation returned `ENOENT` while threads created the
same private trace filename. Fifty repetitions reproduced sixteen failures
in the sandbox and fifteen outside it. Exclusive creation followed by opening
the existing file avoids that initial-creation race; the same fifty repetitions
pass. No missing-directory retry, file-security check, concurrency count or
assertion was relaxed. Original diagnostics remain in the new packet. This is
an observed local filesystem behavior, not a claim about every filesystem.

Seven retained actual process checks pass with nine dispatches, two lookups,
nine effects and complete cleanup. Offline replay validates the observations;
28 mutations of saved data reject separately. They are development checks,
not frozen fault-family coverage, baseline performance or qualification. At that
checkpoint B3 still needed owned stack/quiescence and registration coordination;
the following slice implements those. Fault and F14 adapters remain prerequisites
for qualification-arm registration.

## B3 owned stack and registration, 2026-10-23

The relay now waits after the real delivery-reservation commit and before broker
publication while a separate harness controller reads committed immutable jobs
and registers native mock keys/contracts. Waiting holds no database transaction
or row lock. Fresh request generations bind schema, relay PID and commit xid;
stale, foreign, malformed, refused or absent acknowledgements fail closed.
Workflow children and replayed reservations use this same gate. Workers and
advancers receive no mock admin or independent ledger access.

The owned stack retains every process generation, checks complete process-group
quiescence, stops the controller, closes provider admission and drains calls and
held commits before final observations and schema/configuration removal. Review
found an unrequested clean exit could be hidden by replacement; that exit now
remains an integrity failure. Its actual-process regression passes.

The first integration fixture incorrectly included the HTTP mock admin port in
the provider mapping. Configuration refused the fourth endpoint before execution;
the fixture now supplies only payments, CRM and email. The corrected twelve-case
run passed, as did the added unrequested-exit regression. Selected refusal cases
retain process-error markers and unexpected SIGTERM exits during interpreter
shutdown; they are expected failure checks, not clean execution claims.

The first offline replay incorrectly required a refused relay to exit with code
1. It now checks the actual retained process-error/unexpected-exit records. The
counterexample driver also needed to recognize RegistrationError as a protocol
refusal. Original diagnostics remain. This does not change runtime decisions.

The [owned-stack development packet](dev-baseline-owned-stack-20261023/REVIEW.md)
records selected cases, source and full-regression status. Its fixture uses real
local HTTP mocks directly; complete B3 Toxiproxy fault drivers and F14 shared
normalization remain. Gate timings include deliberate registration pauses and
must not be presented as a performance comparison. No comparison arm or final
qualification is registered or started by this slice.

Final verification passes: 1537 Python tests (849 Phase A, 656 integration,
21 isolated security, nine API, two scripted-agent) and 51 console tests,
plus typecheck/lint/build. Integration took 995.262 seconds without skips.
All thirteen final-source retained cases pass, including four explicit negative
integrity checks, with eleven dispatches, ten effects and complete cleanup of
34 process generations. Offline replay passes and rejects 33 saved-data
mutations. Earlier seven process cases, five B3 oracle cases and seventeen
SafeAct rows replay unchanged; all 300 older packet artifacts remain unchanged.

## Comparison adapters and STOP 2 review, 2026-10-23

All B0–B3 common-runner adapters are registered. B0 retains actual memory-only
process acceptance and bounded retries; B1 uses durable queued jobs with fresh
payment keys; B2 reuses native payment keys; B3 retains its independent fencing,
lookup and human-review implementation. Actual capabilities determine N/A before
outcomes. Missing weaker guards remain observed failures or explicit unobserved
labels. No baseline gains fabricated SafeAct persistence or audit evidence.

Saved-data replay validates 375 selected development rows from 670 rows across
eight retained runs. All selected applicable rows have complete cleanup and no
harness/integrity errors. SafeAct covers 28 declared labels and B3 all 25
applicable labels. Their native acknowledgement-loss evidence includes claim,
fence and outcome stages, plus concurrent refusal while the original fence is
active. Weaker-arm labels that cannot be independently attributed remain
uncredited; raw duplicate effects and pending states remain available. These
development captures span revisions and cannot establish a frozen comparison
or performance advantage. Twenty-five new saved-data mutations are rejected.

Development found partial parked-marker publication and a race between owned
shutdown signaling and recording membership. Atomic publication and recording
shutdown intent before signaling fix them; unsuccessful exit proof still fails.
Original diagnostics remain. Workflow accounting now leaves a rejected inverse
pending unless actual committed escalation exists. F13/F12 inverse targeting,
F06/F07 lookup ordering, F05/F08 kill ownership and F16 achievable-workload
declarations were corrected from source and contract inspection. Held-out
outcomes were never run or used to tune these changes.

The first final integration run exposed a compatibility regression in a generic
read-only unacked decoder; its existing API was restored while reserved queue,
DB and ownership checks remain mandatory for privileged fault drivers. The full
corrected run passes 693 tests in 1288.132 seconds without skips. A flaky API
error test used a short hex secret that could match a random UUID; a distinctive
synthetic value retains the no-leak assertion without changing API behavior.
Final Phase A passes all 901 tests. With 21 security, nine API and two scripted
agent tests, all 1626 unique Python tests pass. All 37 console unit and 14 browser
tests, typecheck, lint and build also pass.

CI now starts its required Toxiproxy service with Linux host networking so it can
reach loopback mock servers; the proxy API binds loopback. Both workflows parse,
but hosted CI has not run. Exact manifest reconstruction proves these two YAML
files were the only pinned-source changes after integration began; no Python
SUT bytes changed. Phase A passed against the final amended pin.

The [STOP 2 packet](dev-baseline-adapter-20261023/REVIEW.md) records 470 archived
source files plus four exact base-only references, and verifies that all 364
artifacts in nine earlier packets remain unchanged. The frozen candidate pins
1200 cases, including 60 controls and 120 held-out combinations, plus an 80-case
sample. Calibration will produce a separate verified slot amendment after
approval. No frozen smoke, calibration or full execution, real Stripe/model
call, staging, commit or push occurred. Work pauses at STOP 2 for explicit final
run authorization.

## Runtime connection pooling integration, 2026-10-24

The author authorized runtime integration after the local prototype comparison,
while explicitly reserving pre-run and big-run authorization. The exact starting
tree matched the earlier sealed 470-file snapshot plus four base references and
its passing 1626 Python / 51 console baseline.

Bounded native psycopg leases now cover the API, worker, relay, reconciler,
advancer, threaded demo and internal common SafeAct admission engine. Pools
belong to one process and one authenticated role. Prefork workers create them
after fork. SSE releases its lease each poll. Native type checks and protected
SQL remain unchanged, including claim/fence/outcome commit acknowledgements.
Fresh observer/retry connections remain after uncertainty, avoiding a nested
checkout from a one-connection pool. MCP admission and metrics retain fresh
connections. Automatic statement preparation remains enabled.

Returning leases rolls back unfinished work and restores session settings,
authorization, cursors, listeners, temporary tables, sequence state and session
advisory locks. Cleanup does not prove a business commit rolled back. Broken
or unresettable sessions are discarded; liveness replacement occurs only before
business code starts and never replays business work.

Focused tests caught a read-probe rollback clearing Psycopg's prepared cache;
the probe now uses autocommit while idle. The cache test uses repeated committed
requests; rollback of genuinely unfinished work still clears cache normally.
Concurrent TestClient consumers exposed shared application lifespan ownership:
one client's shutdown initially closed another's pool. Lifespans now borrow one
app owner, and a later lifespan can lazily create a new owner. Existing workflow
tests and a dedicated lifecycle regression pass without relaxed assertions.

The first full integration pass succeeded on all 709 tests. An additional
controlled probe then reproduced a shutdown/reset race: an in-flight reset could
return a live idle connection after pool disposal. Lease return now serializes
with close and invalidates late returns. The deterministic test checks both the
closed native connection and removal of the PostgreSQL backend. All 17 pool
integration tests and the final complete 710-test integration suite pass. The
initial successful pass and failing diagnostic are retained with their exact
source. The initial Phase A signer socket was sandbox-blocked; a complete
socket-enabled invocation passed. Nothing was skipped.

Final regression: 1650 unique Python tests (908 Phase A, 710 integration,
21 isolated security, nine API, two scripted agent), 37 console unit and
14 browser tests, typecheck, lint and build pass. Final integration took
1315.863 seconds with zero skips.

A separate balanced local benchmark calls the actual ExecutionWorker.execute
with the integrated pool scope, including liveness and full reset costs. Four
hundred measured operations plus 128 warmups completed with one provider call
and one effect per operation, zero errors and independently committed SUCCEEDED
states. At concurrency 1, median latency was 96.79 ms fresh versus 44.67 ms pooled;
at concurrency 4, 131.01 versus 67.31 ms. Each configuration has 100 measured
samples. These contemporaneous measurements exclude public API/authentication
and queue/relay scheduling and do not establish production capacity or stable
p99. They must not be combined with earlier separate-session medians. B0–B3
were not modified; their differing connection strategies preclude interpreting
this as cross-arm performance evidence.

Only source hashes were amended in the frozen candidate (231 backend files).
All scenario, contract, timing, generator, oracle, coverage, applicability and
scoring digests remain unchanged; no held-out outcomes were run. All 519
artifacts in twelve earlier sealed packets remain unchanged. The
[integration packet](dev-pooling-integration-20261024/REVIEW.md) preserves source,
regressions, original diagnostics, local timing and cleanup evidence. The
execution ledger remains absent. No pre-run, frozen smoke, calibration, big
run, real provider/model call, staging, commit or push occurred. Wait for the
author's explicit instruction before pre-run.
