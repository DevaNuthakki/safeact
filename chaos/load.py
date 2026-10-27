"""Load test (M5.13): open-loop arrivals through the real API, to saturation.

    .venv/bin/python -m chaos.load --out docs/phase-e/load-v1

The deployed shape, on one machine, in a disposable schema (dropped at the
end), each component its own OS process and database role:
- the API (one uvicorn process), relay, reconciler, advancer and a prefork
  Celery worker with ``--workers`` processes (Redis DB 0, refused unless
  empty, flushed at the end);
- the HTTP mock providers (``--plain``: honest unless a fault is set) in
  their own process, reached by the worker only through Toxiproxy, which
  adds a fixed latency to every response (``--latency-ms``);
- the effects ledger is read from the providers' harness-only admin API.

The client runs in this process: asyncio, open loop. Request i of a step is
sent at t0 + i/rate whatever earlier requests are doing, so a slow system
is measured as slow instead of slowing the client down. Requests rotate
over 8 requester sessions (one tenant, exact grants), with a mix of 3
payments refunds : 1 CRM order : 1 email. After a step's arrivals stop, it
drains until every accepted operation is terminal (or ``--drain`` passes).

Steps (``--steps``) use the v1 contracts, which declare no F16 limits. Then:
- ``limited``: payments refunds on the v2 contract (25 dispatches/s, burst
  25) above that rate: the token bucket and the queue interacting;
- ``breaker``: the same contract at a lower rate, with a burst of 429s
  from the payments mock part-way through: backoff and the breaker
  (5 consecutive failures, 30 s cool-down) under load.

Measured per step: accepted/s and terminal/s; p50/p95/p99 of submit ->
accepted (client clock), accepted -> claimed (queue wait: relay, broker,
worker pickup) and accepted -> terminal (both PostgreSQL clock), and
submit -> terminal as their per-operation sum; HTTP statuses; terminal
states. PostgreSQL, sampled every second: wait events, lock waits and
active statements of the run's roles; per step: pg_stat_statements top
statements (when the server preloads it, as deploy/compose.yaml does),
deadlocks from pg_stat_database, and lock-timeout, serialization and
deadlock errors counted by class from the server log (never stored as
text). Each sample also records the offset between this host's clock and
PostgreSQL's: a step whose offset moved by more than CLOCK_TOLERANCE_MS
had a clock jump and its database-clock latencies are flagged.

At the end: projection drift, and from the ledger the effects per
operation (any duplicate or any effect without a SUCCEEDED operation fails
the run, exit 1); claims refused by reason and breaker events.

Limits (also written to config.json): one laptop, loopback, Docker Desktop
PostgreSQL, Redis and Toxiproxy, mock providers with a fixed latency, one
API process, no TLS.
"""

import argparse
import asyncio
import gzip
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import uuid

from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

import httpx2 as httpx
import psycopg
import redis

from psycopg import sql

from api.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from chaos import environment
from chaos.quiescence import stop_processes
from chaos.toxiproxy import Toxiproxy
from core.connector_profiles import LIMITED_PROFILES, MOCK_PROFILES
from deploy.demo.bootstrap import (
    DemoAccount, Environment, credential_lines, rebuild, teardown_statements,
    write_credentials,
)
from deploy.evaluation.agent_v1.secret_scan import scan
from deploy.stack.processes import ProcessStack
from storage.database import local_admin_settings, local_redis_url
from storage.state_projection import projection_drift


ROOT = Path(__file__).resolve().parents[1]
TENANT = "tenant-load"
SESSIONS = 8
RESOURCES = 50
ACCOUNTS = {"payments": "mock-payments-account", "crm": "mock-crm-account",
            "email": "mock-email-account"}
MIX = ("payments", "payments", "payments", "crm", "email")
TERMINAL = ("SUCCEEDED", "FAILED_CONFIRMED", "ESCALATED_UNRESOLVED",
            "CANCELLED_BEFORE_CLAIM")
REDIS_DB = 0
# Above the qualification slots (21000-21062) inside Toxiproxy's published range.
PROXY_PORTS = {"payments": 21090, "crm": 21091, "email": 21092}
CLIENT_IN_FLIGHT_CAP = 4000
CLOCK_TOLERANCE_MS = 250
METRICS = ("safeact_claims_refused_total", "safeact_dispatch_outcomes_total",
           "safeact_relay_published_total", "safeact_reconciler_decisions_total",
           "safeact_loop_errors_total", "safeact_lookups_total",
           "safeact_http_requests_total")
ERROR_CLASSES = (
    ("deadlock", "deadlock detected"),
    ("lock_timeout", "canceling statement due to lock timeout"),
    ("serialization", "could not serialize access"),
    ("statement_timeout", "canceling statement due to statement timeout"),
    ("idle_in_transaction_timeout", "idle-in-transaction"),
)
LIMITS = ("one laptop; loopback; Docker Desktop PostgreSQL, Redis and Toxiproxy; "
          "mock providers with a fixed added latency; one API process; no TLS")


def usernames():
    return [f"load-requester-{n}" for n in range(1, SESSIONS + 1)]


def grants():
    rows = []
    for n in range(RESOURCES):
        rows += [("mock-payments", ACCOUNTS["payments"], "refund_payment",
                  f"ch_load_{n}", version) for version in ("v1", "v2")]
        rows.append(("mock-crm", ACCOUNTS["crm"], "create_replacement_order",
                     f"case_load_{n}", "v1"))
        rows.append(("mock-email", ACCOUNTS["email"],
                     "send_refund_confirmation", f"cust_load_{n}", "v1"))
    return tuple(rows)


def load_accounts():
    rows = grants()
    return tuple(DemoAccount(TENANT, name, f"Load Requester {n}", ("requester",),
                             grants=rows)
                 for n, name in enumerate(usernames(), 1))


def request_body(step, index, kind, contract="v1"):
    n = index % RESOURCES
    if kind == "payments":
        action, payload = "refund_payment", {
            "charge_id": f"ch_load_{n}", "amount_minor": 100 + index % 900}
    elif kind == "crm":
        action, payload = "create_replacement_order", {
            "case_id": f"case_load_{n}", "sku": f"sku_{index % 97}"}
    elif kind == "email":
        action, payload = "send_refund_confirmation", {
            "customer_id": f"cust_load_{n}", "template": "refund_done"}
    else:
        raise ValueError(kind)
    return {"request_key": f"load-{step}-{index}", "action_type": action,
            "provider_account_id": ACCOUNTS[kind], "contract_version": contract,
            "payload": payload}


def percentiles(values):
    """Nearest-rank p50/p95/p99 and max, in milliseconds; None when empty."""
    if not values:
        return {"n": 0, "p50": None, "p95": None, "p99": None, "max": None}
    ordered = sorted(values)

    def rank(p):
        return round(ordered[max(0, -(-len(ordered) * p // 100) - 1)], 1)
    return {"n": len(ordered), "p50": rank(50), "p95": rank(95),
            "p99": rank(99), "max": round(ordered[-1], 1)}


# Literals and parameter numbers vary per call; a statement's shape does not.
_LITERALS = re.compile(r"'(?:[^']|'')*'|\b\d+\b|\$\d+")


def statement_shape(text, schema=None, width=160):
    """A statement's shape: no values, no schema name, at most ``width`` chars."""
    text = " ".join((text or "").split())
    if schema:
        text = text.replace(f'"{schema}"', "{schema}").replace(schema, "{schema}")
    return _LITERALS.sub("?", text)[:width]


def error_counts(lines):
    """Server log ERROR/FATAL lines by class; only counts leave this function."""
    counts = Counter()
    for line in lines:
        if "ERROR:" not in line and "FATAL:" not in line:
            continue
        counts["errors"] += 1
        for name, marker in ERROR_CLASSES:
            if marker in line:
                counts[name] += 1
                break
    return {name: counts[name] for name in ("errors", *(n for n, _ in ERROR_CLASSES))}


def clock_offset_range(samples):
    """Spread (ms) of host-minus-database clock offsets across ``samples``."""
    offsets = [s["clock_offset_ms"] for s in samples
               if s.get("clock_offset_ms") is not None]
    if not offsets:
        return None
    return round(max(offsets) - min(offsets), 1)


def statements_delta(before, after, schema, top=10):
    """Top statements by execution time between two pg_stat_statements snapshots."""
    rows = []
    for queryid, (calls, total_ms, query) in after.items():
        old_calls, old_ms, _ = before.get(queryid, (0, 0.0, None))
        if calls > old_calls:
            rows.append({"calls": calls - old_calls,
                         "total_ms": round(total_ms - old_ms, 1),
                         "mean_ms": round((total_ms - old_ms) / (calls - old_calls), 3),
                         "statement": statement_shape(query, schema)})
    rows.sort(key=lambda row: row["total_ms"], reverse=True)
    return rows[:top]


def summarize_step(name, rate, seconds, arrivals_seconds, records, rows, samples):
    """One step's figures from client records and database rows."""
    accepted = [r for r in records if r.get("status") in (200, 201)]
    joined = [(record, rows[record["request_key"]]) for record in accepted
              if record["request_key"] in rows]
    done = [(record, row) for record, row in joined
            if row["state"] in TERMINAL and row["terminal"] is not None]
    queue = [(row["claimed"] - row["admitted"]) * 1000 for _, row in joined
             if row["claimed"] is not None]
    to_terminal = [(row["terminal"] - row["admitted"]) * 1000 for _, row in done]
    end_to_end = [record["accepted_ms"] + (row["terminal"] - row["admitted"]) * 1000
                  for record, row in done]
    first = min((row["admitted"] for _, row in joined), default=None)
    last = max((row["terminal"] for _, row in done), default=None)
    span = last - first if first is not None and last is not None else None
    admitted_span = max((r["sent_s"] + r["accepted_ms"] / 1000 for r in accepted
                         if "sent_s" in r), default=None)
    mine =[s for s in samples if s["step"] == name]
    waits = Counter()
    statements = Counter()
    for sample in mine:
        waits.update(sample["wait_events"])
        statements.update(sample["statements"])
    clock_spread = clock_offset_range(mine)
    return {
        "step": name, "target_rate": rate, "seconds": seconds,
        "scheduled": len(records),
        "arrivals_seconds": round(arrivals_seconds, 2),
        "client_lag_ms": percentiles([r["lag_ms"] for r in records if "lag_ms" in r]),
        "http_status": dict(Counter(str(r.get("status") or r.get("error"))
                                    for r in records)),
        "error_codes": dict(Counter(r["code"] for r in records if r.get("code"))),
        "accepted": len(accepted),
        # Offered: arrivals as scheduled. Admitted: accepted responses over
        # the time until the last of them came back, so a queueing API is
        # not credited with the offered rate.
        "offered_per_second": round(len(records) / arrivals_seconds, 2)
        if arrivals_seconds else None,
        "admitted_per_second": round(len(accepted) / admitted_span, 2)
        if admitted_span else None,
        "terminal": len(done),
        "terminal_per_second": round(len(done) / span, 2) if span else None,
        "states": dict(Counter(row["state"] for _, row in joined)),
        "extra_claims": sum(max(0, row["claims"] - 1) for _, row in joined),
        "latency_ms": {
            "submit_to_accepted": percentiles([r["accepted_ms"] for r in accepted]),
            "queue_wait_accepted_to_claimed": percentiles(queue),
            "accepted_to_terminal": percentiles(to_terminal),
            "submit_to_terminal": percentiles(end_to_end),
        },
        "sampled": {
            "samples": len(mine),
            "max_active": max((s["active"] for s in mine), default=0),
            "max_lock_waits": max((s["lock_waits"] for s in mine), default=0),
            "samples_with_lock_waits": sum(1 for s in mine if s["lock_waits"]),
            "wait_events": dict(waits.most_common(12)),
            "active_statements": dict(statements.most_common(8)),
        },
        "load_1_max": max((s["load"]["load_1"] for s in mine), default=None),
        "clock_offset_spread_ms": clock_spread,
        "clock_jump": clock_spread is not None and clock_spread > CLOCK_TOLERANCE_MS,
    }


def verdict(result):
    """Safety findings that fail the run; usefulness is reported, not judged."""
    failures = []
    if result["projection_drift"]:
        failures.append(f"projection drift: {result['projection_drift']} operations")
    ledger = result["ledger"]
    if ledger["duplicate_effects"]:
        failures.append(f"duplicate effects: {ledger['duplicate_effects']}")
    if ledger["effects"] != ledger["succeeded_operations"]:
        failures.append(f"effects {ledger['effects']} != SUCCEEDED operations "
                        f"{ledger['succeeded_operations']}")
    return failures


# -- runtime ------------------------------------------------------------

def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Sampler(threading.Thread):
    """Once a second: load, clock offset, wait events, lock waits, statements."""

    def __init__(self, roles, schema):
        super().__init__(name="load-sampler", daemon=True)
        self.roles = list(roles)
        self.schema = schema
        self.samples = []
        self.step = None
        self._stop = threading.Event()

    def run(self):
        with psycopg.connect(**local_admin_settings(), autocommit=True) as db:
            while not self._stop.wait(1.0):
                before = time.time()
                database = db.execute(
                    "SELECT extract(epoch FROM clock_timestamp())").fetchone()[0]
                after = time.time()
                activity = db.execute(
                    "SELECT coalesce(wait_event_type, 'CPU'), "
                    "coalesce(wait_event, 'running'), state, query "
                    "FROM pg_stat_activity WHERE usename = ANY(%s) "
                    "AND state <> 'idle'", (self.roles,)).fetchall()
                lock_waits = db.execute(
                    "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a "
                    "USING (pid) WHERE NOT l.granted AND a.usename = ANY(%s)",
                    (self.roles,)).fetchone()[0]
                self.samples.append({
                    "step": self.step, "load": environment.sample(),
                    "clock_offset_ms": round(((before + after) / 2 - float(database)) * 1000, 1),
                    "active": len(activity), "lock_waits": lock_waits,
                    "wait_events": dict(Counter(f"{t}:{e}" for t, e, _, _ in activity)),
                    "statements": dict(Counter(
                        statement_shape(q, self.schema, 100)
                        for _, _, state, q in activity if state == "active")),
                })

    def stop(self):
        self._stop.set()
        self.join(timeout=5)


class Statements:
    """pg_stat_statements in a schema of its own, for this run only."""

    SCHEMA = "safeact_load_stats"

    def __init__(self, admin, roles):
        self.admin = admin
        self.roles = list(roles)
        self.available = False
        self.reason = None

    def __enter__(self):
        preload = self.admin.execute("SHOW shared_preload_libraries").fetchone()[0]
        if "pg_stat_statements" not in preload:
            self.reason = "pg_stat_statements is not preloaded by this server"
            return self
        with self.admin.transaction():
            self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(
                sql.Identifier(self.SCHEMA)))
            self.admin.execute(sql.SQL(
                "CREATE EXTENSION pg_stat_statements SCHEMA {}").format(
                sql.Identifier(self.SCHEMA)))
        self.available = True
        return self

    def snapshot(self):
        if not self.available:
            return {}
        rows = self.admin.execute(sql.SQL(
            "SELECT s.queryid, s.calls, s.total_exec_time, s.query "
            "FROM {}.pg_stat_statements s JOIN pg_roles r ON r.oid = s.userid "
            "WHERE r.rolname = ANY(%s) AND s.queryid IS NOT NULL"
        ).format(sql.Identifier(self.SCHEMA)), (self.roles,)).fetchall()
        return {queryid: (calls, total, query) for queryid, calls, total, query in rows}

    def __exit__(self, *exc):
        if self.available:
            with self.admin.transaction():
                self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(
                    sql.Identifier(self.SCHEMA)))


def postgres_log(since, until):
    """Lines the dev PostgreSQL container logged in [since, until] (unix s)."""
    try:
        container = subprocess.run(
            ["docker", "compose", "--env-file", ".env", "-f", "deploy/compose.yaml",
             "ps", "-q", "postgres"], cwd=ROOT, capture_output=True, text=True,
            timeout=20).stdout.strip()
        if not container:
            return None
        stamp = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")
        output = subprocess.run(
            ["docker", "logs", "--since", stamp(since), "--until", stamp(until),
             container], capture_output=True, text=True, timeout=60)
        return (output.stdout + output.stderr).splitlines()
    except (OSError, subprocess.SubprocessError):
        return None


def database_counters(db):
    row = db.execute(
        "SELECT deadlocks, xact_commit, xact_rollback "
        "FROM pg_stat_database WHERE datname = current_database()").fetchone()
    return dict(zip(("deadlocks", "commits", "rollbacks"), row))


def sign_in(base_url, username, password):
    with httpx.Client(base_url=base_url, timeout=10, trust_env=False) as http:
        response = http.post("/auth/login", json={"username": username,
                                                  "password": password})
        response.raise_for_status()
        return ({name: response.cookies[name] for name in (SESSION_COOKIE, CSRF_COOKIE)},
                response.json()["csrf_token"])


async def drive(base_url, sessions, step, rate, seconds, *, kinds=MIX,
                contract="v1", at=None):
    """Open-loop arrivals; one record per scheduled request.

    ``at`` is an optional (offset seconds, callable) run once mid-step.
    """
    total = int(rate * seconds)
    records = []
    limits = httpx.Limits(max_connections=256, max_keepalive_connections=256)
    clients = []
    for cookies, csrf in sessions:
        client = httpx.AsyncClient(base_url=base_url, timeout=30, limits=limits,
                                   trust_env=False, headers={CSRF_HEADER: csrf})
        # httpx never sends Secure cookies over http; a browser does on localhost.
        for name, value in cookies.items():
            client.cookies.set(name, value)
        clients.append(client)
    in_flight = set()
    start = time.monotonic()

    async def one(index):
        kind = kinds[index % len(kinds)]
        body = request_body(step, index, kind, contract)
        record = {"step": step, "index": index, "kind": kind,
                  "request_key": body["request_key"]}
        sent = time.monotonic()
        record["sent_s"] = round(sent - start, 4)
        record["lag_ms"] = round((sent - start - index / rate) * 1000, 1)
        try:
            response = await clients[index % len(clients)].post("/actions", json=body)
            record["status"] = response.status_code
            if response.status_code in (200, 201):
                record["operation_id"] = response.json()["operation_id"]
            elif response.headers.get("content-type", "").startswith("application/json"):
                record["code"] = (response.json().get("error") or {}).get("code")
        except httpx.HTTPError as error:
            record["status"] = None
            record["error"] = type(error).__name__
        record["accepted_ms"] = round((time.monotonic() - sent) * 1000, 2)
        records.append(record)

    try:
        fired = at is None
        for index in range(total):
            delay = start + index / rate - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if not fired and time.monotonic() - start >= at[0]:
                fired = True
                await asyncio.to_thread(at[1])
            if len(in_flight) >= CLIENT_IN_FLIGHT_CAP:
                records.append({"step": step, "index": index, "status": None,
                                "error": "client_in_flight_cap",
                                "request_key": f"load-{step}-{index}"})
                continue
            task = asyncio.ensure_future(one(index))
            in_flight.add(task)
            task.add_done_callback(in_flight.discard)
        arrivals_seconds = time.monotonic() - start
        if in_flight:
            await asyncio.gather(*in_flight)
    finally:
        for client in clients:
            await client.aclose()
    return records, arrivals_seconds


def step_rows(db, schema, step):
    query = sql.SQL(
        "SELECT o.request_key, extract(epoch FROM o.created_at), "
        "  extract(epoch FROM min(t.created_at) FILTER (WHERE t.to_state = 'CLAIMED')), "
        "  extract(epoch FROM max(t.created_at) FILTER (WHERE t.to_state = ANY(%s))), "
        "  p.state, count(*) FILTER (WHERE t.to_state = 'CLAIMED') "
        "FROM {s}.safeact_operations o "
        "JOIN {s}.safeact_state_projection p USING (operation_id) "
        "LEFT JOIN {s}.safeact_operation_transitions t USING (operation_id) "
        "WHERE o.request_key LIKE %s GROUP BY o.operation_id, p.state"
    ).format(s=sql.Identifier(schema))
    return {key: {"admitted": float(admitted),
                  "claimed": float(claimed) if claimed is not None else None,
                  "terminal": float(terminal) if terminal is not None else None,
                  "state": state, "claims": claims}
            for key, admitted, claimed, terminal, state, claims in db.execute(
                query, (list(TERMINAL), f"load-{step}-%"))}


def drain(db, schema, step, expected, timeout):
    """Wait until ``expected`` operations of ``step`` are terminal; return seconds."""
    query = sql.SQL(
        "SELECT count(*) FROM {s}.safeact_operations o "
        "JOIN {s}.safeact_state_projection p USING (operation_id) "
        "WHERE o.request_key LIKE %s AND p.state = ANY(%s)"
    ).format(s=sql.Identifier(schema))
    start = time.monotonic()
    while True:
        done = db.execute(query, (f"load-{step}-%", list(TERMINAL))).fetchone()[0]
        if done >= expected or time.monotonic() - start > timeout:
            return round(time.monotonic() - start, 1), done >= expected
        time.sleep(0.5)


def scrape(port):
    try:
        text = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=5,
                         trust_env=False).text
    except httpx.HTTPError as error:
        return {"error": type(error).__name__}
    values = {}
    for line in text.splitlines():
        if line.startswith(METRICS):
            name, value = line.rsplit(" ", 1)
            if float(value):
                values[name] = float(value)
    return values


def ledger_check(admin_url, db, schema):
    """Effects per correlation from the independent ledger (ground truth)."""
    ledger = httpx.get(admin_url + "/ledger", timeout=30, trust_env=False).json()
    per = Counter(correlation for correlation, _ in ledger["effects"])
    succeeded = db.execute(sql.SQL(
        "SELECT count(*) FROM {s}.safeact_state_projection WHERE state = 'SUCCEEDED'"
    ).format(s=sql.Identifier(schema))).fetchone()[0]
    return {"provider_calls": len(ledger["calls"]),
            "effects": sum(per.values()), "correlations": len(per),
            "duplicate_effects": sum(n - 1 for n in per.values() if n > 1),
            "succeeded_operations": succeeded}


def breaker_events(db, schema):
    rows = db.execute(sql.SQL(
        "SELECT event, count(*) FROM {s}.safeact_connector_health_events GROUP BY event"
    ).format(s=sql.Identifier(schema))).fetchall()
    return {event: count for event, count in rows}


def seal(out):
    """artifact-sha256.json in the packets' format: [{path, sha256}], sorted."""
    digests = [{"path": path.name, "sha256": sha256(path.read_bytes()).hexdigest()}
               for path in sorted(out.iterdir())
               if path.is_file() and path.name not in ("artifact-sha256.json", "REVIEW.md")]
    (out / "artifact-sha256.json").write_text(json.dumps(digests, indent=2) + "\n")
    return digests


def run(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    steps = [float(v) for v in args.steps.split(",")]
    plan = [(f"s{n}-{rate:g}rps", rate, MIX, "v1", None)
            for n, rate in enumerate(steps, 1)]
    if args.limited_rate:
        plan.append(("limited", args.limited_rate, ("payments",), "v2", None))
    if args.breaker_rate:
        plan.append(("breaker", args.breaker_rate, ("payments",), "v2",
                     args.breaker_429s))
    config = {
        "steps_per_second": steps, "step_seconds": args.step_seconds,
        "limited_rate": args.limited_rate, "breaker_rate": args.breaker_rate,
        "breaker_429s": args.breaker_429s, "workers": args.workers,
        "latency_ms": args.latency_ms, "poll_seconds": args.poll,
        "drain_timeout_seconds": args.drain, "pause_seconds": args.pause,
        "mix": list(MIX), "resources": RESOURCES, "sessions": SESSIONS,
        "contracts": {"steps": "v1, no F16 limits",
                      "limited": "payments v2: 25 dispatches/s, burst 25",
                      "breaker": "payments v2: breaker 5 failures, 30 s cool-down; "
                                 "backoff base 1 s, cap 60 s"},
        "client": "asyncio + httpx2 in the harness process, open loop, "
                  f"{SESSIONS} sessions, in-flight cap {CLIENT_IN_FLIGHT_CAP}",
        "clock_tolerance_ms": CLOCK_TOLERANCE_MS,
        "limits": LIMITS,
    }
    (out / "config.json").write_text(json.dumps(config, indent=2) + "\n")

    broker = redis.Redis.from_url(local_redis_url(REDIS_DB))
    if broker.dbsize():
        raise SystemExit(f"Redis DB {REDIS_DB} is not empty; refusing to use it")
    env_start = environment.describe()
    _, awake = environment.hold_awake(os.getpid())
    admin = psycopg.connect(**local_admin_settings(), autocommit=True)
    env = Environment.disposable(uuid.uuid4().hex)
    toxiproxy = Toxiproxy()
    private = tempfile.TemporaryDirectory(prefix="safeact-load-")
    stack = sampler = None
    records, summaries = [], []
    try:
        role_passwords, passwords = rebuild(
            admin, env, profiles=MOCK_PROFILES + LIMITED_PROFILES,
            accounts=load_accounts())
        credentials = Path(private.name) / "credentials"
        write_credentials(credentials, credential_lines(env, role_passwords))
        upstream = {kind: free_port() for kind in ("payments", "crm", "email", "admin")}
        for kind, port in PROXY_PORTS.items():
            toxiproxy.create(f"safeact-load-{kind}", port, upstream[kind])
            toxiproxy.toxic(f"safeact-load-{kind}", "latency",
                            latency=args.latency_ms, jitter=0)
        metrics_ports = {c: free_port() for c in ("worker", "relay", "reconciler", "api")}
        stack = ProcessStack(
            credentials=credentials, redis_db=REDIS_DB,
            queue="safeact-load-" + uuid.uuid4().hex[:6],
            api_port=free_port(), provider_ports=upstream, plain_providers=True,
            poll_seconds=args.poll, concurrency=args.workers,
            components=("providers", "worker", "relay", "reconciler", "advancer", "api"),
            extra_env={f"SAFEACT_STACK_{kind.upper()}_URL": f"http://127.0.0.1:{port}"
                       for kind, port in PROXY_PORTS.items()},
            metrics_ports=metrics_ports, log_dir=Path(private.name) / "logs",
        )
        stack.start()
        sessions = [sign_in(stack.base_url, name, passwords[name]) for name in usernames()]
        set_faults = lambda kind, switches: httpx.post(
            f"{stack.admin_url}/faults/{kind}", json=switches, timeout=5,
            trust_env=False).raise_for_status()

        with Statements(admin, env.login_roles) as statements:
            sampler = Sampler(env.login_roles, env.schema)
            sampler.start()
            # Warm-up: every process has handled each kind before measuring.
            sampler.step = "warmup"
            warm, _ = asyncio.run(drive(stack.base_url, sessions, "warmup", 5, 2))
            drain(admin, env.schema, "warmup", len(warm), 60)

            for name, rate, kinds, contract, burst in plan:
                at = None
                if burst:
                    at = (args.step_seconds / 3,
                          lambda: set_faults("payments", {"rate_limit_next": burst}))
                time.sleep(args.pause)
                sampler.step = name
                counters = database_counters(admin)
                before = statements.snapshot()
                wall = time.time()
                step_records, arrivals = asyncio.run(drive(
                    stack.base_url, sessions, name, rate, args.step_seconds,
                    kinds=kinds, contract=contract, at=at))
                accepted = sum(1 for r in step_records if r.get("status") in (200, 201))
                drained, complete = drain(admin, env.schema, name, accepted, args.drain)
                sampler.step = "idle"
                after_counters = database_counters(admin)
                log = postgres_log(wall - 1, time.time() + 1)
                summary = summarize_step(name, rate, args.step_seconds, arrivals,
                                         step_records, step_rows(admin, env.schema, name),
                                         sampler.samples)
                summary.update(
                    contract=contract, kinds=sorted(set(kinds)),
                    drain_seconds=drained, drained=complete,
                    database={f"{k}_delta": after_counters[k] - counters[k]
                              for k in counters},
                    top_statements=statements_delta(before, statements.snapshot(),
                                                    env.schema),
                    server_log_errors=error_counts(log) if log is not None else None,
                )
                summaries.append(summary)
                records += step_records
                print(json.dumps({k: summary[k] for k in (
                    "step", "admitted_per_second", "terminal_per_second",
                    "drain_seconds", "states", "clock_jump")}), flush=True)
            sampler.stop()
            statements_note = statements.reason

        metrics = {component: scrape(port) for component, port in metrics_ports.items()}
        drift = projection_drift(admin, env.schema)
        ledger = ledger_check(stack.admin_url, admin, env.schema)
        health = breaker_events(admin, env.schema)
        operations = admin.execute(sql.SQL(
            "SELECT count(*) FROM {s}.safeact_operations"
        ).format(s=sql.Identifier(env.schema))).fetchone()[0]
        unfinished = dict(admin.execute(sql.SQL(
            "SELECT state, count(*) FROM {s}.safeact_state_projection "
            "WHERE state <> ALL(%s) GROUP BY state"
        ).format(s=sql.Identifier(env.schema)), (list(TERMINAL),)).fetchall())
    finally:
        if sampler is not None and sampler.is_alive():
            sampler.stop()
        try:
            if stack is not None:
                stop_processes(stack)
        finally:
            for kind in PROXY_PORTS:
                toxiproxy.delete(f"safeact-load-{kind}")
            toxiproxy.close()
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                          "WHERE usename = ANY(%s)", (list(env.roles),))
            with admin.transaction():
                for statement in teardown_statements(admin, env):
                    admin.execute(statement)
            admin.close()
            broker.flushdb()
            broker.close()
            private.cleanup()

    result = {
        "finished": datetime.now(timezone.utc).isoformat(),
        "steps": summaries,
        "projection_drift": len(drift),
        "ledger": ledger,
        "unfinished": unfinished,
        "breaker_events": health,
        "operations": operations,
        "metrics": metrics,
        "pg_stat_statements": statements_note or "available",
        "clock_jumps": [s["step"] for s in summaries if s["clock_jump"]],
    }
    # The relay republishes every still-claimable operation on each pass;
    # under a backlog the claim refuses the copies (already_claimed).
    published = metrics["relay"].get("safeact_relay_published_total")
    result["relay_published_per_operation"] = (
        round(published / operations, 2) if published and operations else None)
    result["safety_failures"] = verdict(result)
    (out / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    (out / "environment.json").write_text(json.dumps({
        "start": env_start, "end": environment.describe(),
        "awake": environment.awake_status(awake),
    }, indent=2) + "\n")
    with gzip.open(out / "samples.jsonl.gz", "wt", encoding="utf-8") as handle:
        for sample in sampler.samples:
            handle.write(json.dumps(sample, sort_keys=True) + "\n")
    with gzip.open(out / "requests.jsonl.gz", "wt", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    seal(out)
    findings = scan([out])
    if findings:
        raise SystemExit(f"Secret-shaped text in the output: {findings}")
    print(json.dumps({k: result[k] for k in (
        "projection_drift", "ledger", "unfinished", "breaker_events",
        "clock_jumps", "safety_failures")}), flush=True)
    return 1 if result["safety_failures"] else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--steps", default="10,20,40,80,120,160")
    parser.add_argument("--step-seconds", type=float, default=30)
    parser.add_argument("--limited-rate", type=float, default=50)
    parser.add_argument("--breaker-rate", type=float, default=10)
    parser.add_argument("--breaker-429s", type=int, default=8)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--latency-ms", type=int, default=20)
    parser.add_argument("--poll", type=float, default=0.2)
    parser.add_argument("--drain", type=float, default=600)
    parser.add_argument("--pause", type=float, default=5)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
