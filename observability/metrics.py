"""Prometheus metrics for every SafeAct process (M5.7).

Each process exports on SAFEACT_METRICS_PORT (host SAFEACT_METRICS_HOST,
default 127.0.0.1) when that variable is set; nothing is exported
otherwise. The prefork Celery worker uses prometheus_client's
multiprocess mode (PROMETHEUS_MULTIPROC_DIR), so every child's counts are
exported by the parent.

Labels come from a fixed allow-list of low-cardinality names. No metric
ever carries a tenant, a principal, an operation id, a provider key or a
resource id (a test enforces the label names; values come only from
SafeAct's own vocabularies).

Process-local counters say what this process did since it started. The
durable picture (operations per state, the claimable backlog and its age,
open breakers, waiting retries) is read from PostgreSQL at scrape time by
DatabaseCollector, through the protected function safeact_metrics() (0020),
so it survives restarts and agrees with the log.
"""

import os

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Histogram,
    REGISTRY,
    start_http_server,
)
from prometheus_client.core import GaugeMetricFamily


ALLOWED_LABELS = frozenset({
    "connector", "kind", "reason", "decision", "state", "loop",
    "method", "route", "status",
})
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10,
                   30)

DISPATCH_OUTCOMES = Counter(
    "safeact_dispatch_outcomes", "Dispatch outcomes, by connector and kind.",
    ["connector", "kind", "reason"],
)
DISPATCH_SECONDS = Histogram(
    "safeact_dispatch_seconds", "Provider dispatch latency.", ["connector"],
    buckets=LATENCY_BUCKETS,
)
LOOKUPS = Counter(
    "safeact_lookups", "Reconciler lookups, by connector and evidence kind.",
    ["connector", "kind"],
)
LOOKUP_SECONDS = Histogram(
    "safeact_lookup_seconds", "Provider lookup latency.", ["connector"],
    buckets=LATENCY_BUCKETS,
)
CLAIMS_REFUSED = Counter(
    "safeact_claims_refused", "Claims refused, by reason.", ["reason"],
)
DECISIONS = Counter(
    "safeact_reconciler_decisions", "Reconciler moves, by decision and state.",
    ["decision", "state"],
)
RELAYED = Counter(
    "safeact_relay_published", "Outbox deliveries the relay published.",
)
LOOP_ERRORS = Counter(
    "safeact_loop_errors", "Errors kept from killing a loop.", ["loop"],
)
HTTP_REQUESTS = Counter(
    "safeact_http_requests", "API requests, by method, route and status.",
    ["method", "route", "status"],
)
HTTP_SECONDS = Histogram(
    "safeact_http_request_seconds", "API request latency.", ["route"],
    buckets=LATENCY_BUCKETS,
)

METRICS = (
    DISPATCH_OUTCOMES, DISPATCH_SECONDS, LOOKUPS, LOOKUP_SECONDS,
    CLAIMS_REFUSED, DECISIONS, RELAYED, LOOP_ERRORS, HTTP_REQUESTS,
    HTTP_SECONDS,
)

# Durable gauges from safeact_metrics(): name -> (help, label or None).
DATABASE_GAUGES = {
    "operations": ("Operations by current state.", "state"),
    "claimable": ("Operations a worker may claim now.", None),
    "oldest_claimable_seconds": ("Age of the oldest claimable operation.",
                                 None),
    "breakers_open": ("Provider breakers open now.", None),
    "retries_waiting": ("Protected retries waiting for their backoff.", None),
}


class DatabaseCollector:
    """Durable gauges read from PostgreSQL at scrape time."""

    def __init__(self, connect, schema):
        self._connect = connect
        self._schema = schema

    def collect(self):
        from psycopg import sql

        families = {
            name: GaugeMetricFamily(
                f"safeact_{name}", text, labels=[label] if label else [])
            for name, (text, label) in DATABASE_GAUGES.items()
        }
        try:
            connection = self._connect()
        except Exception:
            return
        try:
            rows = connection.execute(sql.SQL(
                "SELECT metric, label, value FROM {}.safeact_metrics()"
            ).format(sql.Identifier(self._schema))).fetchall()
            connection.rollback()
        except Exception:
            rows = []
        finally:
            connection.close()
        for metric, label, value in rows:
            family = families.get(metric)
            if family is None:
                continue
            family.add_metric([label] if DATABASE_GAUGES[metric][1] else [],
                              float(value))
        yield from families.values()

    def describe(self):
        return []


def serve(collector=None, *, env=None):
    """Export metrics for this process if SAFEACT_METRICS_PORT is set."""

    env = os.environ if env is None else env
    port = env.get("SAFEACT_METRICS_PORT")
    if not port:
        return None
    host = env.get("SAFEACT_METRICS_HOST", "127.0.0.1")
    registry = REGISTRY
    if env.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
    if collector is not None:
        registry.register(collector)
    server, _ = start_http_server(int(port), addr=host, registry=registry)
    return server


def refused_reason(error):
    """A claim refusal's reason, from SafeAct's own exception names."""

    return {
        "NotYetDue": "not_due",
        "ProviderLimited": "provider_limited",
        "StaleTransition": "already_claimed",
        "IllegalTransition": "not_claimable",
        "RetryBudgetExhausted": "budget_exhausted",
    }.get(type(error).__name__, "other")
