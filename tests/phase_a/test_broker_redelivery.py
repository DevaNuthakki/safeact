"""Counterexamples for direct early-redelivery evidence and cleanup."""

import json
import os
import tempfile
import threading
import unittest

from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from chaos.broker_redelivery import DispatchRedelivery, unacked_message
from chaos.coverage import observed
from chaos.runner import FaultDriver, Timeline
from chaos.scenarios import generate
from connectors.mock_support import HeldCall
from deploy.stack import broker_trace as trace
from tests.phase_a.test_qualification_broker import IDENTITY, OP, QUEUE, SCHEMA, envelope


TAG = str(uuid4())


def unacked():
    message = json.loads(envelope())
    message["properties"]["delivery_tag"] = TAG
    return json.dumps([message, QUEUE, QUEUE]).encode()


def evidence():
    common = dict(IDENTITY, source="broker", queue=QUEUE, pid=20, redelivered=False)
    original = dict(common, pid=31)
    duplicate = dict(common, pid=32, redelivered=True)
    store = dict(IDENTITY, redis_db=2, queue=QUEUE, delivery_tag=TAG)
    transport = dict(store, source="broker_transport", method="QoS.restore_by_tag")
    sample = dict(IDENTITY, source="database", event="db_broker_dispatching", state="DISPATCHING",
        claim_id="claim", attempt_id="attempt", transition_seq=2, lease_active=True,
        provider="payments", correlation_sha256="digest")
    client = dict(source="http_client", request_id="request", pid=31, method="POST", path="/refund",
        provider="payments", correlation_sha256="digest")
    provider = dict(source="provider", provider_call_id="call", method="refund",
        path=None, provider="payments", correlation_sha256="digest")
    return dict(admissions=[dict(index=0, local_id=OP, changed=False)], observation_finished_monotonic_ns=120,
        broker=[dict(common, event="publish_started", monotonic_ns=1),
                dict(common, event="publish_returned", monotonic_ns=2),
                dict(common, event="task_received", monotonic_ns=3),
                dict(original, event="task_started", monotonic_ns=4),
                dict(common, event="task_received", redelivered=True, monotonic_ns=50),
                dict(duplicate, event="task_started", monotonic_ns=51),
                dict(duplicate, event="task_returned", task_status="not_claimable", monotonic_ns=52),
                dict(original, event="task_returned", task_status="succeeded", monotonic_ns=100)],
        database=[dict(sample, sample_started_ns=30, sample_finished_ns=31, monotonic_ns=31),
                  dict(sample, sample_started_ns=70, sample_finished_ns=71, monotonic_ns=71)],
        http=[dict(client, event="http_start", monotonic_ns=10),
              dict(client, event="http_response", status_code=200, monotonic_ns=90)],
        ledger=[dict(provider, event="provider_start", monotonic_ns=11),
                dict(provider, event="provider_end", monotonic_ns=80)],
        timeline=[dict(store, source="broker_store", event="broker_unacked_observed",
                       sample_started_ns=20, sample_finished_ns=21, monotonic_ns=22),
                  dict(transport, event="broker_restore_requested", monotonic_ns=40),
                  dict(transport, event="broker_restore_returned", monotonic_ns=60)])


LABEL = "broker_redelivered_during_dispatch"


class RedeliveryCoverageTests(unittest.TestCase):
    def test_real_identity_restoration_and_concurrent_body_are_credited(self):
        self.assertIn(LABEL, observed(evidence()))

    def test_each_transport_link_is_required(self):
        for index in range(3):
            raw = evidence(); raw["timeline"].pop(index)
            self.assertNotIn(LABEL, observed(raw))

    def test_both_database_samples_are_required(self):
        for index in range(2):
            raw = evidence(); raw["database"].pop(index)
            self.assertNotIn(LABEL, observed(raw))

    def test_changed_dispatch_or_uncommitted_state_is_not_credit(self):
        for key, value in (("state", "UNKNOWN"), ("claim_id", "other"), ("attempt_id", "other"),
                ("transition_seq", 3), ("provider", "email"), ("correlation_sha256", "other"),
                ("delivery_id", "other")):
            raw = evidence(); raw["database"][1][key] = value
            self.assertNotIn(LABEL, observed(raw))

    def test_fresh_publication_or_fabricated_redelivery_flag_is_not_credit(self):
        for key, value in (("task_id", str(uuid4())), ("delivery_id", str(uuid4())),
                           ("queue", "other"), ("redelivered", False), ("pid", 31)):
            raw = evidence(); raw["broker"][5][key] = value
            self.assertNotIn(LABEL, observed(raw))

    def test_publication_and_both_receives_must_be_observed(self):
        for index in (0, 1, 2, 3, 4, 5):
            raw = evidence(); raw["broker"].pop(index)
            self.assertNotIn(LABEL, observed(raw))

    def test_http_provider_and_process_identity_cannot_be_inferred(self):
        for change in (lambda r: r.update(http=[]), lambda r: r.update(ledger=[]),
                lambda r: r["http"][0].update(pid=99),
                lambda r: r["ledger"][1].update(monotonic_ns=50),
                lambda r: r["http"][1].update(monotonic_ns=50)):
            raw = evidence(); change(raw)
            self.assertNotIn(LABEL, observed(raw))

    def test_post_dispatch_or_cleanup_redelivery_does_not_count(self):
        for changes in (dict(observation_finished_monotonic_ns=60), dict(admissions=[])):
            raw = evidence(); raw.update(changes)
            self.assertNotIn(LABEL, observed(raw))
        raw = evidence(); raw["database"][1].update(sample_started_ns=100, sample_finished_ns=101, monotonic_ns=101)
        self.assertNotIn(LABEL, observed(raw))

    def test_safety_outcome_does_not_determine_whether_the_fault_occurred(self):
        raw = evidence(); raw["broker"][6]["task_status"] = "succeeded"
        self.assertIn(LABEL, observed(raw))

    def test_redis_database_tag_and_method_must_match(self):
        for key, value in (("redis_db", 0), ("delivery_tag", "other"), ("method", "send_task"), ("task_id", "other")):
            raw = evidence(); raw["timeline"][2][key] = value
            self.assertNotIn(LABEL, observed(raw))


def fixture():
    rows = [dict(IDENTITY, event="publish_returned", redelivered=False, pid=20, monotonic_ns=1),
            dict(IDENTITY, event="task_started", redelivered=False, pid=31, monotonic_ns=2)]
    client = SimpleNamespace(connection_pool=SimpleNamespace(connection_kwargs={"db": 2}),
        hgetall=Mock(return_value={TAG.encode(): unacked()}), hget=Mock(return_value=unacked()))
    calls, events = [], []
    def restore(tag):
        calls.append(("restore", tag))
        rows.extend([dict(IDENTITY, event="task_started", redelivered=True, pid=32, monotonic_ns=3),
            dict(IDENTITY, event="task_returned", redelivered=True, pid=32, monotonic_ns=4, task_status="not_claimable")])
    driver = DispatchRedelivery(client, 2, QUEUE, lambda: {OP},
        lambda message: calls.append(("db", message)), lambda: rows, restore,
        lambda label, **details: events.append(dict(details, event=label)))
    held = HeldCall(); held.started.set()
    return driver, held, calls, events


class RedeliveryDriverTests(unittest.TestCase):
    def test_only_actual_unacked_message_is_restored_between_independent_samples(self):
        driver, held, calls, events = fixture()
        driver.run(held, threading.Event())
        self.assertEqual([c[0] for c in calls], ["db", "restore", "db"])
        self.assertEqual(calls[1][1], TAG)
        self.assertTrue(held._release.is_set())
        self.assertEqual([e["event"] for e in events], ["broker_unacked_observed",
            "broker_restore_requested", "broker_restore_returned", "broker_redelivery_finished"])
        self.assertNotIn("body", json.dumps(events))

    def test_wrong_database_and_queue_cannot_be_targeted(self):
        driver, held, calls, events = fixture()
        for db, queue in ((0, QUEUE), (2, "safeact-stack"), (3, QUEUE)):
            with self.assertRaises(RuntimeError):
                DispatchRedelivery(driver.client, db, queue, None, None, None, None, None)

    def test_foreign_unacked_envelope_or_tag_is_refused(self):
        for tag, value in ((b"bad", unacked()), (TAG.encode(), b"[]"),
                           (str(uuid4()).encode(), unacked())):
            with self.assertRaises((ValueError, KeyError)):
                unacked_message(tag, value, QUEUE)
        with self.assertRaises(ValueError):
            unacked_message(TAG.encode(), unacked(), "other")

    def test_only_admitted_still_running_body_can_be_selected(self):
        driver, held, calls, events = fixture()
        driver.admitted = lambda: set()
        self.assertIsNone(driver._select())
        driver.admitted = lambda: {OP}
        rows = list(driver.observations())
        rows.append(dict(IDENTITY, event="task_returned", redelivered=False, pid=31))
        driver.observations = lambda: rows
        self.assertIsNone(driver._select())

    def test_changed_or_missing_unacked_store_entry_stops_restoration(self):
        driver, held, calls, events = fixture()
        driver.client.hget.return_value = None
        with self.assertRaises(RuntimeError):
            driver.run(held, threading.Event())
        self.assertEqual([c[0] for c in calls], ["db"])
        self.assertTrue(held._release.is_set())

    def test_dispatch_sample_failure_releases_provider_without_restoring(self):
        driver, held, calls, events = fixture()
        driver.dispatching = Mock(side_effect=RuntimeError("not dispatching"))
        with self.assertRaises(RuntimeError):
            driver.run(held, threading.Event())
        self.assertFalse(calls)
        self.assertTrue(held._release.is_set())

    def test_transport_failure_is_not_a_completed_action_and_releases_hold(self):
        driver, held, calls, events = fixture()
        driver.restore = Mock(side_effect=OSError("restore failed"))
        with self.assertRaises(OSError):
            driver.run(held, threading.Event())
        self.assertTrue(held._release.is_set())
        self.assertNotIn("broker_restore_returned", [e["event"] for e in events])

    def test_cancelled_run_always_releases_provider(self):
        driver, held, calls, events = fixture()
        cancelled = threading.Event(); cancelled.set()
        with self.assertRaises(RuntimeError):
            driver.run(held, cancelled)
        self.assertTrue(held._release.is_set())
        self.assertFalse(calls)

    def test_duplicate_dispatch_is_reported_as_failure_instead_of_green(self):
        driver, held, calls, events = fixture()
        restore = driver.restore
        def unsafe(tag):
            restore(tag)
            driver.observations()[-1]["task_status"] = "succeeded"
        driver.restore = unsafe
        with self.assertRaisesRegex(RuntimeError, "not refused at claim"):
            driver.run(held, threading.Event())
        self.assertTrue(held._release.is_set())

    def test_fault_driver_tracks_hold_completion_and_propagates_worker_error(self):
        held = HeldCall()
        arm = SimpleNamespace(redeliver_during_dispatch=Mock(side_effect=RuntimeError("failed restore")))
        slot = SimpleNamespace(mocks={"payments": SimpleNamespace(hold_next=Mock(return_value=held))}, proxies={})
        driver = FaultDriver(slot, Timeline(), arm=arm)
        driver.apply({"kind": "broker_redelivery", "provider": "payments"})
        for timer in driver.timers:
            timer.join(1)
        driver.cancel(); driver.release_holds()
        self.assertTrue(driver.errors)
        self.assertTrue(held._release.is_set())
        self.assertEqual(driver.required - driver.completed, {"broker_redelivery"})


class RedeliveryProtocolTests(unittest.TestCase):
    def test_postrun_records_only_allowlisted_success_result(self):
        signals = SimpleNamespace(task_received=Mock(), task_prerun=Mock(), task_postrun=Mock())
        app = object()
        task = SimpleNamespace(app=app, name=trace.TASK, request=SimpleNamespace(delivery_info={"redelivered": True}))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SAFEACT_STACK_BROKER_TRACE_DIR": directory}):
            trace.install(app, SCHEMA, QUEUE, role="worker", signals=signals)
            callback = signals.task_postrun.connect.call_args.args[0]
            callback(task=task, task_id=IDENTITY["task_id"], args=[IDENTITY[k] for k in trace.IDS[:3]],
                     state="SUCCESS", retval="not_claimable")
            callback(task=task, task_id=IDENTITY["task_id"], args=[IDENTITY[k] for k in trace.IDS[:3]],
                     state="FAILURE", retval="private result")
            rows = trace.read_trace(directory, 0)
        self.assertEqual(rows[0]["task_status"], "not_claimable")
        self.assertNotIn("task_status", rows[1])
        self.assertNotIn("private result", json.dumps(rows))

    def test_result_fields_cannot_be_attached_to_other_events_or_leak_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = trace.BrokerTrace(directory, SCHEMA)
            for event, status in (("task_started", "not_claimable"), ("task_returned", "private result")):
                with self.assertRaises(ValueError):
                    writer(event, **IDENTITY, queue=QUEUE, redelivered=True, task_status=status)

    def test_variant_cycle_preserves_reviewed_loss_cases_and_adds_all_providers(self):
        self.assertTrue(all(generate("dev", "F08", i, 7).faults[0]["kind"] == "broker_loss" for i in range(8)))
        cases = [generate("dev", "F08", i, 7) for i in range(8, 12)]
        self.assertEqual({case.connector for case in cases}, {"payments", "crm", "email"})
        self.assertTrue(all(case.contract_version == "q-v2-inflight"
            and case.faults[0]["kind"] == "broker_redelivery" and case.completion_achievable for case in cases))
