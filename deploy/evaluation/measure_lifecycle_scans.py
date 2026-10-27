"""Measure lifecycle scan cost on seeded history: log scans vs projection.

Disposable schema on the local dev database only; dropped afterwards.
Seeds 20,000 completed operations plus small in-flight groups, then
runs EXPLAIN (ANALYZE, BUFFERS) on the exact SQL the protected
functions use. Prints a JSON report.

Requires SAFEACT_RUN_DB_INTEGRATION=1 and
SAFEACT_RUN_DISPOSABLE_DB_TESTS=1.

    .venv/bin/python -m deploy.evaluation.measure_lifecycle_scans
"""

import json
import os
import platform
import statistics

from psycopg import sql

from storage import durable_lifecycle_sql as lsql
from tests.integration.lifecycle_fixture import DisposableLifecycleDatabase


COMPLETED = 20000
REPEATS = 7

GROUPS = {
    "done": (COMPLETED, "SUCCEEDED", 100000),
    "dispatching": (20, "DISPATCHING", 50000),
    "unknown": (20, "UNKNOWN", 50000),
    "fresh": (10, None, 10),
}


def plan(admin, query):
    report = admin.execute(
        sql.SQL("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ") + query
    ).fetchone()[0][0]

    nodes = []

    def walk(node):
        label = node["Node Type"]
        if node.get("Relation Name"):
            label += " on " + node["Relation Name"]
        if node.get("Index Name"):
            label += " using " + node["Index Name"]
        nodes.append(label)
        for child in node.get("Plans", ()):
            walk(child)

    walk(report["Plan"])
    return report["Execution Time"], nodes


def measure(admin, query):
    plan(admin, query)  # warm cache
    runs = [plan(admin, query) for _ in range(REPEATS)]
    times = sorted(t for t, _ in runs)
    return {
        "median_ms": round(statistics.median(times), 3),
        "min_ms": round(times[0], 3),
        "max_ms": round(times[-1], 3),
        "plan": runs[-1][1],
    }


def main():
    if not (
        os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
        and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1"
    ):
        raise SystemExit("Explicit disposable PostgreSQL opt-in required")

    db = DisposableLifecycleDatabase()
    try:
        db.start()
        db.seed_history(GROUPS)
        admin, schema = db.admin, db.schema

        operation = admin.execute(sql.SQL(
            "SELECT operation_id FROM {} WHERE request_key = 'done-777'"
        ).format(db.table("safeact_operations"))).fetchone()[0]

        # "log_*": the 0003/0007 anti-joins over the transition log (the
        # scans before 0017); the others read the current-state projection.
        queries = {}
        for prefix, claimable, in_state in (
            ("log_", lsql.log_claimable_query, lsql.log_in_state_query),
            ("", lsql.claimable_query, lsql.in_state_query),
        ):
            queries.update({
                prefix + "claimable_limit_100": claimable(
                    schema, sql.Literal(100)
                ),
                prefix + "dispatching_scan_limit_100": in_state(
                    schema, sql.Literal("DISPATCHING"), sql.Literal(100)
                ),
                prefix + "unknown_scan_limit_100": in_state(
                    schema, sql.Literal("UNKNOWN"), sql.Literal(100)
                ),
            })
        queries.update({
            "current_state_by_operation": sql.SQL(
                "SELECT * FROM {} WHERE operation_id = {}"
            ).format(
                db.table(lsql.CURRENT_STATE_VIEW),
                sql.Literal(str(operation)),
            ),
        })

        counts = {
            table: db.count(table) for table in (
                "safeact_operations",
                "safeact_operation_transitions",
                "safeact_state_projection",
            )
        }

        report = {
            "server": admin.execute("SHOW server_version").fetchone()[0],
            "client": platform.platform(),
            "seed": {k: v[:2] for k, v in GROUPS.items()},
            "rows": counts,
            "repeats": REPEATS,
            "results": {
                name: measure(admin, query)
                for name, query in queries.items()
            },
        }
        print(json.dumps(report, indent=2))
    finally:
        db.cleanup()


if __name__ == "__main__":
    main()
