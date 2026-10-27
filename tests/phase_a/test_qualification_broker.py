"""Broker observations, protocol round trips and message-loss counterexamples."""

import base64
import json
import os
import signal
import tempfile
import threading
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from chaos.broker_loss import QueuedLoss
from chaos.coverage import observed
from chaos.runner import FaultDriver, Timeline
from chaos.scenarios import generate
from deploy.stack import broker_trace as trace


QUEUE = "safeact-q0-abcdef"
SCHEMA = "safeact_it_" + "a" * 32
OP, OUTBOX, DELIVERY, TASK = [str(uuid4()) for _ in range(4)]
IDENTITY = dict(zip(trace.IDS, (OP, OUTBOX, DELIVERY, TASK)))


def envelope(**changes):
    message = {"headers": {"task": trace.TASK, "id": TASK},
        "properties": {"body_encoding": "base64", "correlation_id": TASK,
                       "delivery_info": {"routing_key": QUEUE}},
        "content-type": "application/json", "content-encoding": "utf-8",
        "body": base64.b64encode(json.dumps([[OP, OUTBOX, DELIVERY], {}, {}]).encode()).decode()}
    message.update(changes)
    return json.dumps(message).encode()


def broker_run():
    first = dict(IDENTITY, source="broker", queue=QUEUE, pid=20, redelivered=False)
    second = dict(first, delivery_id=str(uuid4()), task_id=str(uuid4()))
    return dict(admissions=[dict(index=0, local_id=OP, changed=False)],
        observation_finished_monotonic_ns=100,
        broker=[dict(first, event="publish_started", monotonic_ns=10),
                dict(first, event="publish_returned", monotonic_ns=20),
                dict(second, event="publish_started", monotonic_ns=60),
                dict(second, event="publish_returned", monotonic_ns=70),
                dict(second, event="task_received", monotonic_ns=65, pid=30),
                dict(second, event="task_started", monotonic_ns=80, pid=31)],
        database=[dict(source="database", event="db_broker_pending", state="RECORDED",
            operation_id=OP, outbox_id=OUTBOX, delivery_id=DELIVERY,
            sample_started_ns=25, sample_finished_ns=29, monotonic_ns=29)],
        timeline=[dict(event="broker_consumer_stopped", returncode=-9, monotonic_ns=1),
            dict(event="broker_consumers_disconnected", redis_db=2, monotonic_ns=2),
            dict(event="broker_relay_stopped", monotonic_ns=24, requested_signal=int(signal.SIGSTOP),
                stop_signal=int(signal.SIGSTOP), wait_status=(int(signal.SIGSTOP) << 8) | 0x7f),
            dict(event="broker_messages_flushed", source="broker_store", redis_db=2, queue=QUEUE,
                messages=[IDENTITY], before_count=1, after_count=0, database_size_after=0,
                acknowledged=True, sample_started_ns=30, sample_finished_ns=40, monotonic_ns=45)])


class TraceTests(unittest.TestCase):
    def test_disabled_observer_has_no_signal_side_effects(self):
        signals = Mock()
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(trace.install(object(), "safeact_demo", QUEUE, role="worker", signals=signals))
        self.assertFalse(signals.mock_calls)

    def test_trace_is_private_and_rejects_duplicate_or_corrupt_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = trace.BrokerTrace(directory, SCHEMA)
            writer("publish_started", **IDENTITY, queue=QUEUE, redelivered=False)
            self.assertEqual(len(trace.read_trace(directory, 0)), 1)
            path = next(Path(directory).glob("broker-*.jsonl"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text(path.read_text() * 2)
            with self.assertRaises(RuntimeError):
                trace.read_trace(directory, 0)
            with self.assertRaises(RuntimeError):
                trace.BrokerTrace(directory, "safeact_demo")

    def test_task_payloads_and_unexpected_fields_cannot_enter_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = trace.BrokerTrace(directory, SCHEMA)
            with self.assertRaises(ValueError):
                writer("task_started", **IDENTITY, queue=QUEUE, redelivered=False, payload="private")


    def test_failed_signal_observer_marks_harness_without_raising(self):
        signals = SimpleNamespace(task_received=Mock(), task_prerun=Mock(), task_postrun=Mock())
        app = object()
        task = SimpleNamespace(app=app, name=trace.TASK, request=SimpleNamespace(delivery_info={}))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SAFEACT_STACK_BROKER_TRACE_DIR": directory}):
            trace.install(app, SCHEMA, QUEUE, role="worker", signals=signals)
            callback = signals.task_prerun.connect.call_args.args[0]
            with patch.object(trace.BrokerTrace, "__call__", side_effect=OSError("private detail")), patch.object(trace.os, "write") as write:
                callback(task=task, task_id=TASK, args=[OP, OUTBOX, DELIVERY])
            write.assert_called_once_with(2, trace.ERROR)

    def test_unrelated_task_is_not_observed(self):
        signals = SimpleNamespace(task_received=Mock(), task_prerun=Mock(), task_postrun=Mock())
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SAFEACT_STACK_BROKER_TRACE_DIR": directory}):
            trace.install(object(), SCHEMA, QUEUE, role="worker", signals=signals)
            callback = signals.task_prerun.connect.call_args.args[0]
            callback(task=SimpleNamespace(app=object(), name=trace.TASK), task_id=TASK, args=[OP, OUTBOX, DELIVERY])
            self.assertEqual(trace.read_trace(directory, 0), [])

    def test_parent_receive_uses_request_object_and_retains_redelivery_metadata(self):
        signals = SimpleNamespace(task_received=Mock(), task_prerun=Mock(), task_postrun=Mock())
        app = object()
        request = SimpleNamespace(name=trace.TASK, task=SimpleNamespace(app=app),
            args=[OP, OUTBOX, DELIVERY], id=TASK, delivery_info={"redelivered": True})
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SAFEACT_STACK_BROKER_TRACE_DIR": directory}):
            trace.install(app, SCHEMA, QUEUE, role="worker", signals=signals)
            signals.task_received.connect.call_args.args[0](request=request)
            rows = trace.read_trace(directory, 0)
        self.assertEqual([(r["event"], r["redelivered"]) for r in rows], [("task_received", True)])

    def test_envelope_retains_only_ids(self):
        row = trace.queued_message(envelope(), QUEUE)
        self.assertEqual({key: row[key] for key in trace.IDS}, IDENTITY)
        self.assertEqual(set(row), set(trace.IDS))

    def test_foreign_or_malformed_envelopes_are_rejected(self):
        variants = [b"bad json", envelope(body="invalid base64!"), envelope(**{"content-type": "pickle"})]
        message = json.loads(envelope()); message["headers"]["task"] = "foreign"; variants.append(json.dumps(message).encode())
        for value in variants:
            with self.subTest(value=value[:20]), self.assertRaises((ValueError, KeyError)):
                trace.queued_message(value, QUEUE)


class CoverageTests(unittest.TestCase):
    def test_queued_removal_and_fresh_publication_delivery_are_observed(self):
        self.assertIn("broker_messages_lost", observed(broker_run()))

    def test_empty_flush_or_command_alone_is_not_message_loss(self):
        for key, value in (("messages", []), ("before_count", 0), ("acknowledged", False),
                ("after_count", 1), ("database_size_after", 1), ("redis_db", 0)):
            raw = broker_run(); raw["timeline"][-1][key] = value
            self.assertNotIn("broker_messages_lost", observed(raw))

    def test_missing_or_claimed_database_observation_is_not_credit(self):
        for change in (lambda r: r.update(database=[]), lambda r: r["database"][0].update(state="CLAIMED"),
                lambda r: r["database"][0].update(delivery_id="other"),
                lambda r: r["database"][0].update(sample_finished_ns=50)):
            raw = broker_run(); change(raw)
            self.assertNotIn("broker_messages_lost", observed(raw))

    def test_no_consumer_stop_or_disconnect_or_relay_stop_is_not_credit(self):
        for index in range(3):
            raw = broker_run(); raw["timeline"].pop(index)
            self.assertNotIn("broker_messages_lost", observed(raw))
        raw = broker_run(); raw["timeline"][2]["stop_signal"] = int(signal.SIGKILL)
        self.assertNotIn("broker_messages_lost", observed(raw))

    def test_missing_publish_or_delivery_observation_is_not_credit(self):
        for index in range(6):
            raw = broker_run(); raw["broker"].pop(index)
            self.assertNotIn("broker_messages_lost", observed(raw))

    def test_message_already_received_cannot_be_called_queued_loss(self):
        raw = broker_run()
        raw["broker"].append(dict(raw["broker"][0], event="task_received", monotonic_ns=21))
        self.assertNotIn("broker_messages_lost", observed(raw))

    def test_republication_must_be_after_flush_and_use_new_ids(self):
        for change in (lambda r: r["broker"][2].update(monotonic_ns=35),
                lambda r: [e.update(delivery_id=DELIVERY) for e in r["broker"][2:]],
                lambda r: [e.update(task_id=TASK) for e in r["broker"][2:]],
                lambda r: [e.update(operation_id="other") for e in r["broker"][2:]]):
            raw = broker_run(); change(raw)
            self.assertNotIn("broker_messages_lost", observed(raw))

    def test_cleanup_or_unadmitted_messages_never_establish_coverage(self):
        for changes in (dict(observation_finished_monotonic_ns=75), dict(admissions=[])):
            raw = broker_run(); raw.update(changes)
            self.assertNotIn("broker_messages_lost", observed(raw))


class Pipe:
    def __init__(self, client):
        self.client, self.multi_called = client, False
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def watch(self, key): self.client.events.append("watch")
    def lrange(self, *args): return self if self.multi_called else list(self.client.messages)
    def multi(self): self.multi_called = True; self.client.events.append("multi")
    def execute_command(self, *args):
        assert args == ("FLUSHDB", "SYNC")
        self.client.events.append("flush_queued")
    def llen(self, *args): return self
    def dbsize(self): return self
    def execute(self):
        self.client.events.append("execute")
        before, self.client.messages = self.client.messages, []
        return [before, self.client.acknowledged, 0, 0]


def loss_fixture():
    client = SimpleNamespace(messages=[envelope()], events=[], acknowledged=True,
        connection_pool=SimpleNamespace(connection_kwargs={"db": 2}), client_list=Mock(return_value=[]))
    client.lrange = lambda *args: list(client.messages)
    client.pipeline = lambda: Pipe(client)
    process = SimpleNamespace(pid=12345, returncode=None)
    process.poll = lambda: process.returncode
    def kill(*args): process.returncode = -signal.SIGKILL
    stack = SimpleNamespace(processes={"worker": process, "relay": process}, kill=Mock(side_effect=kill),
        spawn=Mock(return_value=SimpleNamespace(pid=12346)))
    emit = Mock()
    def unclaimed(messages): client.events.append("database_checked")
    loss = QueuedLoss(stack, client, 2, QUEUE, lambda: {OP}, unclaimed,
        lambda: [dict(IDENTITY, event="publish_returned")], emit)
    return loss, client, stack, emit


class FaultTests(unittest.TestCase):
    def test_seeded_f08_covers_all_connectors_and_observed_worker_kills(self):
        cases = [generate("dev", "F08", i, 7) for i in range(8)]
        self.assertEqual({c.connector for c in cases}, {"payments", "crm", "email"})
        self.assertTrue(all(c.faults[0] == {"kind": "broker_loss"} for c in cases))
        kills = [f for c in cases for f in c.faults if f["kind"] == "worker_kill"]
        self.assertTrue(kills)
        self.assertTrue(all(f == {"kind": "worker_kill", "point": "after_claim_commit"} for f in kills))

    def test_slot_database_guard_runs_before_any_fault(self):
        loss, client, stack, emit = loss_fixture()
        for db in (0, 1, 9, True):
            with self.subTest(db=db), self.assertRaises(RuntimeError):
                QueuedLoss(stack, client, db, QUEUE, lambda: set(), Mock(), Mock(), emit)
        stack.kill.assert_not_called()
        with patch("chaos.broker_loss.os.getpgrp", return_value=12345), self.assertRaises(RuntimeError):
            loss.begin()
        stack.kill.assert_not_called()

    def test_consumption_gate_kills_owned_worker_and_retains_generation(self):
        loss, client, stack, emit = loss_fixture()
        with patch("chaos.broker_loss.os.getpgid", return_value=12345):
            loss.begin()
        stack.kill.assert_called_once_with("worker", signal.SIGKILL)
        self.assertIn("killed-worker-12345", stack.processes)
        self.assertEqual([c.args[0] for c in emit.call_args_list],
            ["broker_consumer_stopped", "broker_consumers_disconnected"])

    def test_outstanding_blocking_read_must_disconnect_before_admission(self):
        loss, client, stack, emit = loss_fixture()
        client.client_list.return_value = [{"db": 2, "flags": "b"}]
        with patch("chaos.broker_loss.os.getpgid", return_value=12345), \
                patch("chaos.broker_loss.time.monotonic", side_effect=[0, 4]), \
                self.assertRaisesRegex(RuntimeError, "still blocked"):
            loss.begin()

    def test_unclaimed_snapshot_precedes_atomic_flush_and_restart(self):
        loss, client, stack, emit = loss_fixture()
        with patch.object(loss, "_pause_relay"), patch.object(loss, "resume") as resume:
            loss.finish(threading.Event())
        self.assertEqual(client.events, ["watch", "database_checked", "multi", "flush_queued", "execute"])
        self.assertEqual(client.messages, [])
        self.assertEqual(emit.call_args_list[0].args[0], "broker_messages_flushed")
        resume.assert_called_once(); stack.spawn.assert_called_once_with("worker")

    def test_claimed_work_blocks_flush_and_cleanup_still_restarts_consumer(self):
        loss, client, stack, emit = loss_fixture()
        loss.unclaimed = Mock(side_effect=RuntimeError("claimed"))
        with patch.object(loss, "_pause_relay"), patch.object(loss, "resume") as resume, self.assertRaises(RuntimeError):
            loss.finish(threading.Event())
        self.assertNotIn("flush_queued", client.events)
        resume.assert_called_once(); stack.spawn.assert_called_once()

    def test_foreign_queued_task_blocks_database_flush(self):
        loss, client, stack, emit = loss_fixture()
        message = json.loads(envelope()); message["headers"]["task"] = "foreign"
        client.messages = [json.dumps(message).encode()]
        with self.assertRaises(ValueError):
            loss.finish(threading.Event())
        self.assertNotIn("flush_queued", client.events); stack.spawn.assert_called_once()

    def test_failed_flush_acknowledgement_cannot_emit_success(self):
        loss, client, stack, emit = loss_fixture(); client.acknowledged = False
        with patch.object(loss, "_pause_relay"), patch.object(loss, "resume"), self.assertRaises(RuntimeError):
            loss.finish(threading.Event())
        self.assertNotIn("broker_messages_flushed", [c.args[0] for c in emit.call_args_list])

    def test_cancelled_loss_never_flushes_and_restarts_consumer(self):
        loss, client, stack, emit = loss_fixture(); cancelled = threading.Event(); cancelled.set()
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            loss.finish(cancelled)
        self.assertNotIn("flush_queued", client.events); stack.spawn.assert_called_once()

    def test_relay_pause_requires_actual_waitpid_stop_status(self):
        loss, client, stack, emit = loss_fixture()
        stopped = (int(signal.SIGSTOP) << 8) | 0x7f
        with patch("chaos.broker_loss.os.getpgid", return_value=12345), \
                patch("chaos.broker_loss.os.waitpid", return_value=(12345, stopped)), \
                patch("chaos.broker_loss.os.killpg") as kill:
            loss._pause_relay(); loss.resume()
        self.assertEqual([c.args for c in kill.call_args_list], [(12345, signal.SIGSTOP), (12345, signal.SIGCONT)])

    def test_unfinished_required_fault_is_a_cleanup_error(self):
        driver = FaultDriver(None, Timeline()); driver.required.add("broker_loss"); driver.cancel()
        self.assertTrue(any("broker_loss" in error for error in driver.errors))
