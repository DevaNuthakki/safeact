# Observability: structured logs and Prometheus metrics (M5.7)

**Blueprint:**
- §10 "Structured logs, Prometheus metrics, optional OpenTelemetry traces";
- §09 database contention and tail latency (the load test uses these).

OpenTelemetry stays unused: `opentelemetry-api` is pinned by the API lock but nothing calls it (decision 7).

## Exporting

**When and where:**
- Each process exports metrics only when `SAFEACT_METRICS_PORT` is set, on `SAFEACT_METRICS_HOST` (default `127.0.0.1`).
- `ProcessStack(metrics_ports={...})` gives each component its own port. In Compose (M5.14) they are scraped over the internal network and never published.

**The prefork worker:**
- It runs in prometheus_client multiprocess mode, with `PROMETHEUS_MULTIPROC_DIR` set per stack.
- The pool parent exports every child's counts. Forked children never import the worker module again, so only the parent serves.

**The durable picture comes from PostgreSQL:**
- **What:** operations per state, the claimable backlog and its age, open breakers, and waiting retries. Process-local counters reset on restart; these do not.
- **How:** the reconciler process reads them at scrape time through the recovery-role function `safeact_metrics()` (migration 0020). It returns aggregates only.

**The API:** it exports on its own metrics port, not on an API route, so the API's public surface is unchanged.

## Metric catalogue

| Metric | Type | Labels | From |
|---|---|---|---|
| `safeact_dispatch_outcomes_total` | counter | connector, kind (`applied`/`not_applied`/`ambiguous`), reason | worker |
| `safeact_dispatch_seconds` | histogram | connector | worker |
| `safeact_claims_refused_total` | counter | reason (`not_due`, `provider_limited`, `already_claimed`, `not_claimable`, `budget_exhausted`, `other`) | worker |
| `safeact_relay_published_total` | counter | none | relay |
| `safeact_lookups_total` | counter | connector, kind (evidence kind) | reconciler |
| `safeact_lookup_seconds` | histogram | connector | reconciler |
| `safeact_reconciler_decisions_total` | counter | decision, state (target) | reconciler |
| `safeact_loop_errors_total` | counter | loop | relay / reconciler / advancer |
| `safeact_http_requests_total` | counter | method, route (template), status | API |
| `safeact_http_request_seconds` | histogram | route (template) | API |
| `safeact_operations` | gauge (DB) | state | reconciler, via `safeact_metrics()` |
| `safeact_claimable` | gauge (DB) | none | reconciler |
| `safeact_oldest_claimable_seconds` | gauge (DB) | none | reconciler |
| `safeact_breakers_open` | gauge (DB) | none | reconciler |
| `safeact_retries_waiting` | gauge (DB) | none | reconciler |

**Cardinality rule:**
- Labels come from a fixed allow-list (`observability.metrics.ALLOWED_LABELS`).
- No metric carries a tenant, principal, operation, attempt, claim, provider key or resource id. Values come only from SafeAct's own vocabularies: connector ids, kinds, reasons, states, and route templates such as `/actions/{operation_id}`, never the raw path.
- Enforced by `tests/phase_a/test_observability.py` (label names and call sites) and `tests/integration/test_observability_stack.py`, which checks every exported line of every process for UUIDs, tenant names and provider keys.

## Structured logs

**Format:** every process uses `api.logging.JsonFormatter`, one JSON object per line. It redacts:
- values under sensitive keys;
- inline secrets and URL passwords;
- key-shaped values: SafeAct agent keys, OpenAI keys, and Stripe keys and webhook secrets.

**Events:**

| Event | Process | Fields |
|---|---|---|
| `request` | API | request_id, method, path (never the query), status, duration_ms |
| `dispatch_outcome` | worker | operation_id, claim_id, connector, outcome, reason, latency_ms |
| `lookup` | reconciler | operation_id, connector, evidence, note, pass, latency_ms |
| `reconciler_move` | reconciler | operation_id, from_state, to_state, reason, decision, seq |
| `loop_error` | loops | loop, error_type |

**Correlation:** logs carry operation ids, metrics never do. A metric tells you how many; the logs tell you which.

**Leak check:** `test_observability_stack` runs a real refund through the process stack and checks the logs of every process. None contains a role or account password, and the secret scan finds nothing.

## Known limits

- No tracing; correlation is by operation id in the logs.
- There is no request-level latency for submit → terminal. The load test (M5.13) measures it from the database: operation `created_at` against the terminal transition.
- In multiprocess mode, a dead child's counts remain in the directory until the stack stops (prometheus_client's documented behaviour for counters). Gauges are only the database ones, which do not use multiprocess files.
