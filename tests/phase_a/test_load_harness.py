"""The load harness's pure parts (chaos/load.py); no services run."""

import json
import tempfile
import unittest

from pathlib import Path

from chaos import load


def sample(step, offset, load_1=1.0, **extra):
    return dict(step=step, clock_offset_ms=offset, load=dict(load_1=load_1),
                active=extra.get("active", 1), lock_waits=extra.get("lock_waits", 0),
                wait_events=extra.get("wait_events", {}),
                statements=extra.get("statements", {}))


class LoadHarnessTests(unittest.TestCase):

    def test_percentiles_are_nearest_rank(self):
        values = list(range(1, 101))
        self.assertEqual(load.percentiles(values),
                         dict(n=100, p50=50, p95=95, p99=99, max=100))
        self.assertEqual(load.percentiles([7.0])["p99"], 7.0)
        self.assertEqual(load.percentiles([]),
                         dict(n=0, p50=None, p95=None, p99=None, max=None))

    def test_request_bodies_stay_inside_the_granted_resources(self):
        granted = {(action, resource, version) for _, _, action, resource, version
                   in load.grants()}
        for index in range(3 * load.RESOURCES):
            for kind, contract in (("payments", "v1"), ("payments", "v2"),
                                   ("crm", "v1"), ("email", "v1")):
                body = load.request_body("s1", index, kind, contract)
                resource = next(iter(body["payload"].values()))
                self.assertIn((body["action_type"], resource, contract), granted)
                self.assertEqual(body["request_key"], f"load-s1-{index}")
        with self.assertRaises(ValueError):
            load.request_body("s1", 0, "sms")
        self.assertEqual(len(load.load_accounts()), load.SESSIONS)

    def test_statement_shape_drops_values_and_the_schema_name(self):
        shape = load.statement_shape(
            "SELECT * FROM \"safeact_it_load1\".safeact_api_submit($1, 'pw''x', 42)",
            "safeact_it_load1")
        self.assertEqual(shape, "SELECT * FROM {schema}.safeact_api_submit(?, ?, ?)")
        self.assertLessEqual(len(load.statement_shape("x" * 500)), 160)

    def test_server_log_is_reduced_to_counts_by_class(self):
        lines = [
            "2026-10-25 10:00:00 UTC [1] ERROR:  deadlock detected",
            "2026-10-25 10:00:00 UTC [1] DETAIL:  Process 1 waits for ShareLock",
            "2026-10-25 10:00:01 UTC [2] ERROR:  canceling statement due to lock timeout",
            "2026-10-25 10:00:02 UTC [3] ERROR:  could not serialize access due to concurrent update",
            "2026-10-25 10:00:03 UTC [4] ERROR:  SAT31 claim refused: secret-ish detail",
            "2026-10-25 10:00:04 UTC [5] LOG:  checkpoint starting",
        ]
        counts = load.error_counts(lines)
        self.assertEqual(counts, dict(errors=4, deadlock=1, lock_timeout=1,
                                      serialization=1, statement_timeout=0,
                                      idle_in_transaction_timeout=0))
        self.assertTrue(all(isinstance(v, int) for v in counts.values()))

    def test_a_clock_jump_inside_a_step_is_flagged(self):
        steady = [sample("s1", 3.0), sample("s1", 4.5), sample("s1", 2.8)]
        jumped = steady + [sample("s1", 3.0 + load.CLOCK_TOLERANCE_MS + 1)]
        self.assertEqual(load.clock_offset_range(steady), 1.7)
        self.assertIsNone(load.clock_offset_range([sample("s1", None)]))
        rows = {}
        self.assertFalse(load.summarize_step("s1", 10, 1, 1, [], rows, steady)["clock_jump"])
        self.assertTrue(load.summarize_step("s1", 10, 1, 1, [], rows, jumped)["clock_jump"])

    def test_statements_delta_ranks_by_time_spent_in_the_step(self):
        before = {1: (10, 100.0, "SELECT 1"), 2: (5, 50.0, "SELECT $1")}
        after = {1: (12, 104.0, "SELECT 1"), 2: (15, 150.0, "SELECT $1"),
                 3: (1, 9.0, "SELECT 'a'")}
        rows = load.statements_delta(before, after, None)
        self.assertEqual([r["statement"] for r in rows], ["SELECT ?", "SELECT ?", "SELECT ?"])
        self.assertEqual([(r["calls"], r["total_ms"]) for r in rows],
                         [(10, 100.0), (1, 9.0), (2, 4.0)])
        self.assertEqual(rows[0]["mean_ms"], 10.0)

    def test_step_summary_joins_client_records_with_database_times(self):
        records = [
            dict(request_key="load-s1-0", status=201, sent_s=0.0, accepted_ms=5.0, lag_ms=0.1),
            dict(request_key="load-s1-1", status=201, sent_s=0.25, accepted_ms=1750.0,
                 lag_ms=0.2),
            dict(request_key="load-s1-2", status=422, code="SAT23", accepted_ms=3.0,
                 lag_ms=0.0),
            dict(request_key="load-s1-3", status=None, error="ReadTimeout",
                 accepted_ms=30000.0, lag_ms=0.3),
        ]
        rows = {
            "load-s1-0": dict(admitted=100.0, claimed=100.05, terminal=100.2,
                              state="SUCCEEDED", claims=1),
            "load-s1-1": dict(admitted=100.5, claimed=100.6, terminal=None,
                              state="UNKNOWN", claims=2),
        }
        summary = load.summarize_step("s1", 4, 1, 1.0, records, rows,
                                      [sample("s1", 1.0, 3.5, lock_waits=2)])
        self.assertEqual(summary["accepted"], 2)
        # Offered as scheduled; admitted until the last accepted response.
        self.assertEqual(summary["offered_per_second"], 4.0)
        self.assertEqual(summary["admitted_per_second"], 1.0)
        self.assertEqual(summary["terminal"], 1)
        self.assertEqual(summary["states"], {"SUCCEEDED": 1, "UNKNOWN": 1})
        self.assertEqual(summary["http_status"], {"201": 2, "422": 1, "ReadTimeout": 1})
        self.assertEqual(summary["error_codes"], {"SAT23": 1})
        self.assertEqual(summary["extra_claims"], 1)
        latency = summary["latency_ms"]
        self.assertEqual(latency["queue_wait_accepted_to_claimed"]["max"], 100.0)
        self.assertEqual(latency["accepted_to_terminal"]["p50"], 200.0)
        self.assertEqual(latency["submit_to_terminal"]["p50"], 205.0)
        self.assertEqual(latency["submit_to_accepted"]["max"], 1750.0)
        self.assertEqual(summary["sampled"]["max_lock_waits"], 2)
        self.assertEqual(summary["load_1_max"], 3.5)

    def test_verdict_fails_on_drift_duplicates_or_unmatched_effects_only(self):
        clean = dict(projection_drift=0, ledger=dict(
            duplicate_effects=0, effects=10, succeeded_operations=10))
        self.assertEqual(load.verdict(clean), [])
        for change, expected in (
            (dict(projection_drift=2), "projection drift"),
            (dict(ledger=dict(duplicate_effects=1, effects=11,
                              succeeded_operations=10)), "duplicate effects"),
            (dict(ledger=dict(duplicate_effects=0, effects=9,
                              succeeded_operations=10)), "effects 9"),
        ):
            with self.subTest(change=change):
                self.assertTrue(load.verdict({**clean, **change})[0].startswith(expected))

    def test_seal_lists_run_outputs_in_the_packet_format(self):
        with tempfile.TemporaryDirectory() as name:
            out = Path(name)
            for file in ("summary.json", "config.json", "REVIEW.md"):
                (out / file).write_text(file)
            load.seal(out)
            entries = json.loads((out / "artifact-sha256.json").read_text())
        self.assertEqual([e["path"] for e in entries], ["config.json", "summary.json"])
        self.assertTrue(all(set(e) == {"path", "sha256"} and len(e["sha256"]) == 64
                            for e in entries))


if __name__ == "__main__":
    unittest.main()
