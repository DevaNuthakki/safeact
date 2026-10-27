# Load test v1 (M5.13)

Open-loop arrivals through the real API, stepped up past saturation, then the F16
limiter and breaker under load. One laptop, loopback, mock providers. These figures
describe this machine and this configuration only; they are not a capacity claim.

| | |
|---|---|
| Command | `.venv/bin/python -m chaos.load --out docs/phase-e/load-v1` (defaults; [config.json](config.json)) |
| Harness | `chaos/load.py` `sha256: f1a06a3d6a570ad8038cde59087dffb3f8ff8120598bde33ae8a5771b3602570` at run time, on `33242f6` plus the M5.13 changes committed with this packet. After the run, only the seal was changed to the packets' `[{path, sha256}]` format and the secret scan moved after it (`sha256: bf32b8e54ded88f3dafe93cc64244fd2264e1df89a0e134337a507056ddd3aa0`, committed); the seal was rewritten from the same digests |
| Database | development PostgreSQL 17.11 with `pg_stat_statements` preloaded (`deploy/compose.yaml` `sha256: 7ea1287eeb5de6bbfecf8d7aad814f033ea3612ee1c8003cdc5e429c8198e220`) |
| Finished | 2026-10-25 20:23 UTC, about 11 minutes |
| Machine | MacBookPro15,1, i7-8850H, 12 CPUs, 32 GB, macOS 15.4.1, AC power; the run's own `caffeinate -i -w` assertion was observed at the end ([environment.json](environment.json)) |
| Stack | Separate OS processes, one database role each: one API process (uvicorn), relay, reconciler, advancer, Celery prefork worker with **4** processes, HTTP mock providers. Disposable schema, dropped at the end |
| Providers | Honest HTTP mocks (`--plain`) reached by the worker only through Toxiproxy, which adds **20 ms** to every response |
| Client | asyncio + httpx2 in the harness process, 8 requester sessions in one tenant with exact grants. Mix: 3 payments refunds : 1 CRM order : 1 email |
| Steps | 10, 20, 40, 80, 120, 160 requests/s for 30 s each, v1 contracts (no F16 limits). Each step drains until every accepted operation is terminal before the next starts |
| External cost | 0 real provider calls, 0 model calls, $0 |

## Safety at the end of the run

| Check | Result |
|---|---|
| Operations admitted | 14,710 (14,700 measured + 10 warm-up) |
| Provider effects (independent ledger) | 14,709 = SUCCEEDED operations 14,709 |
| Duplicate effects | **0** |
| Projection drift | **0** |
| Operations left non-terminal | 0 |
| FAILED_CONFIRMED | 1 (breaker step, see below; proven not applied, 0 effects) |
| Clock jumps (host vs PostgreSQL offset moved > 250 ms in a step) | none; the largest spread in any step was 5.9 ms |

## Throughput and latency (v1 contracts)

Offered is the scheduled arrival rate. Admitted is accepted responses divided by the
time until the last accepted response came back. Terminal is operations reaching a
terminal state divided by the time from the first admission to the last terminal
transition. Queue wait is accepted → CLAIMED (relay pass, broker, worker pickup).
Latencies are p50 / p95 / p99 in milliseconds.

| Step | Offered/s | Admitted/s | Terminal/s | Submit → accepted | Queue wait | Submit → terminal | Drain after arrivals |
|---|---:|---:|---:|---|---|---|---:|
| 10/s | 10.0 | 10.0 | 10.0 | 16 / 19 / 22 | 138 / 234 / 245 | 192 / 289 / 299 | 0.5 s |
| 20/s | 20.0 | 20.0 | 19.9 | 16 / 20 / 24 | 171 / 267 / 276 | 225 / 323 / 333 | 0.5 s |
| 40/s | 40.0 | 40.0 | 39.8 | 15 / 20 / 24 | 230 / 352 / 384 | 284 / 409 / 439 | 0.5 s |
| 80/s | 80.0 | 80.0 | 47.7 | 20 / 27 / 34 | 10,321 / 19,209 / 20,118 | 10,383 / 19,265 / 20,174 | 20.5 s |
| 120/s | 120.0 | 120.0 | 43.3 | 24 / 49 / 82 | 27,052 / 50,063 / 52,418 | 27,143 / 50,129 / 52,490 | 53.6 s |
| 160/s | 160.0 | 88.4 | 32.1 | 20,020 / 26,404 / 27,437 | 59,046 / 98,697 / 102,101 | 79,141 / 124,311 / 126,617 | 95.8 s |

Every request got 201 in every step. All 14,400 step operations ended SUCCEEDED.

**Reading it:**
- **Execution saturates first**, between 40/s and 80/s: about 40 to 48 operations
  per second complete with 4 worker processes and 20 ms provider latency. Above that
  the backlog waits in the broker. Nothing is lost or duplicated, and queue wait
  grows with the backlog.
- **Admission saturates at the single API process** between 120/s and 160/s. At
  160/s offered, it admitted 88/s and submit → accepted rose to a median of 20 s.
- **Below saturation** (up to 40/s), submit → terminal stays under 0.45 s at p99. Most
  of that is queue wait, which includes the relay's 0.2 s polling pass.
- **Completions fall** from 48/s to 32/s as the offered rate rises past saturation.
  Two causes are measured here, and their shares are not separated:
  - the client, API and workers compete for the same 12 CPUs (1-minute load reached
    10.4);
  - relay republishing: see the next section.

## The relay republishes backlog, and the claim refuses the copies

The relay published **2.25 deliveries per operation** (33,042 for 14,710). The worker
refused **14,135 claims as `already_claimed`**.

`safeact_relay_pending` returns every operation that is still claimable. The relay
publishes up to 100 of them on every 0.2 s pass, with no hold-off since the
operation's last delivery. Below saturation, an operation is claimed before the next
pass, so this never shows. Once the worker falls behind, the head of the backlog is
republished pass after pass.

This is safe by construction. The claim admits exactly one worker, and no step had an
extra claim outside the breaker's retries. It is not free, though: each copy costs a
broker message, a worker pickup and a refused claim transaction.

**Candidate improvement, not evaluated:** republish an operation only after its last
delivery is older than a hold-off, such as the broker visibility timeout. That
changes the system under test after qualification, so it would need its own
sub-step, tests and a disclosed new freeze. It is recorded in
[observations.md](../observations.md).

## Database contention

| Step | Max lock waits | Deadlocks | Lock timeouts | Serialization failures | ERROR lines | Rollbacks | Max active backends |
|---|---:|---:|---:|---:|---:|---:|---:|
| 10/s, 20/s, 40/s | 0 | 0 | 0 | 0 | 0 | 0 | 2-5 |
| 80/s | 0 | 0 | 0 | 0 | 2,832 | 2,733 | 6 |
| 120/s | 0 | 0 | 0 | 0 | 4,736 | 4,686 | 5 |
| 160/s | 0 | 0 | 0 | 0 | 6,563 | 6,586 | 7 |
| limited | 0 | 0 | 0 | 0 | 3,395 | 3,447 | 6 |
| breaker | 1 (one sample) | 0 | 0 | 0 | 799 | 789 | 3 |

**Every server-log ERROR is a refused claim.** Over the steps that logged errors, the
ERROR count (18,325) equals `already_claimed` (14,135) plus `provider_limited`
(4,190), the worker's refused-claim counters. Refused claims raise in SQL and roll
back. That is the intended refusal path, not contention.

The sampled wait events of the run's roles were mostly `Client:ClientRead` (a backend
waiting for the next statement from its component), `CPU` and `IO:WalSync`. They held
no heavyweight lock waits apart from the one breaker sample.

The top statements by time at 160/s (from `pg_stat_statements`, mean per call) were:

| Statement | Mean |
|---|---:|
| `safeact_record_dispatch_outcome` | 3.4 ms |
| `safeact_claim` | 3.2 ms |
| `safeact_api_submit` | 2.7 ms |
| `safeact_begin_dispatch` | 1.6 ms |

At 40/s each of these was about 1 ms. Per-step lists are in
[summary.json](summary.json) (`top_statements`).

**How the counts were taken:**
- Lock-timeout, deadlock and serialization errors were counted by message class from
  the PostgreSQL container log. Only the counts were kept; no log text was stored.
- `pg_stat_database` gave the deadlock and rollback deltas.
- The extension lived in a schema of its own during the run (`safeact_load_stats`)
  and was dropped at the end.

## Limiter and breaker under load (payments v2 contract)

| Step | Offered/s | Terminal/s | Queue wait p50 / p99 | States | Refused claims |
|---|---:|---:|---|---|---|
| limited (payments only, above the 25/s limit) | 50.0 | **25.3** | 14,107 / 30,053 ms | 1,500 SUCCEEDED | `provider_limited`: the token bucket held dispatch to its 25/s rate (burst 25) |
| breaker (10/s, 8 consecutive 429s injected 10 s in) | 10.0 | 2.2 | 109,856 / 123,986 ms | 299 SUCCEEDED, 1 FAILED_CONFIRMED | `breaker_opened` 4, `probe_admitted` 4, `breaker_closed` 1 |

- **Limiter:** the token bucket held dispatch to the contract's 25/s. The rest of the
  load waited as claimable work, and no request was rejected.
- **Breaker:**
  - **What the 429s did:** 8 dispatches were answered 429 (rejected before
    execution). Five consecutive failures opened the breaker for 30 s. Each cool-down
    admitted one probe; the first three met the remaining 429s and reopened it, and
    the fourth succeeded and closed it.
  - **What the reconciler decided:** it proved non-application by lookup 8 times,
    retried 7 operations (the 7 extra claims) and ended 1 as FAILED_CONFIRMED
    (`confirm_not_applied`, 0 effects).
  - **What is not known:** the run's metrics do not record which limit ended that
    operation instead of retrying it, and the disposable schema was dropped at the
    end. A F16 storm ending in proven non-application is the trade-off already
    published for D-F16-0002.
  - **The cost:** the waiting operations stayed queued through four 30 s cool-downs,
    hence the queue wait of about 110 s.

## Limits

- **Environment:** one laptop; loopback only; Docker Desktop PostgreSQL, Redis and
  Toxiproxy; mock providers with a fixed 20 ms latency; one API process; no TLS.
- **Shared CPUs:** the load client runs on the same machine as the system under test,
  and they compete for the same CPUs.
- **Clocks:** latencies after admission use PostgreSQL's clock (transition
  timestamps). Submit → accepted uses the client's monotonic clock. Each figure is a
  difference within one clock, and the host-to-database offset was watched every
  second for jumps.
- **Repeatability:**
  - A first run of the same command (20:01-20:11 UTC) reported "accepted/s" over the
    arrival window. That credits a queueing API with the offered rate, so the harness
    was corrected to report offered and admitted rates, and the run was repeated.
  - The first run's safety results were the same: 0 duplicates, 0 drift, and effects
    equal to SUCCEEDED.
  - Its throughput differed by up to about 20% at saturation: 40.0 / 34.1 / 36.4
    terminal/s at 80 / 120 / 160/s.
  - It is kept outside the repository (below). These figures come from a single
    configuration (4 workers, 20 ms) and are not averaged.

## Files

- [summary.json](summary.json): per-step figures, top statements, end-of-run checks,
  metrics (allow-listed labels only).
- [config.json](config.json) and [environment.json](environment.json).
- [samples.jsonl.gz](samples.jsonl.gz): one database and load sample per second.
- [requests.jsonl.gz](requests.jsonl.gz): one record per scheduled request, holding
  only request keys, statuses and timings.
- [artifact-sha256.json](artifact-sha256.json) seals the run outputs. The output
  passed the secret scan in the harness.
- The superseded first run is in `~/safeact-evidence/docs/phase-e/load-v1-run1/`,
  listed in [../evidence-index.md](../evidence-index.md).
