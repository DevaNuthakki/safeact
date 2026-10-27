"""Exercise the pinned Celery/Kombu protocol with its actual signals, without a server."""

import json
import os
import tempfile
import unittest

from unittest.mock import patch
from uuid import uuid4

from deploy.stack import broker_trace as trace


SCHEMA = "safeact_it_" + "a" * 32
OP, OUTBOX, DELIVERY = [str(uuid4()) for _ in range(3)]


@unittest.skipUnless(os.environ.get("SAFEACT_RUN_BROKER_TESTS") == "1", "explicit broker integration opt-in")
class BrokerSignalTests(unittest.TestCase):
    def test_redis_restores_actual_unacked_identity_and_sets_redelivery_metadata(self):
        from celery import Celery
        import redis
        from chaos.broker_redelivery import restore_unacked, unacked_message
        from storage.database import local_redis_url

        url = local_redis_url(9)
        queue = "safeact-redelivery-test-" + uuid4().hex
        client = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        app = Celery("redis-redelivery-protocol", broker=url, set_as_current=False)
        app.conf.update(task_default_queue=queue, task_ignore_result=True,
                        task_serializer="json", accept_content=["json"])
        tag = None
        try:
            result = app.send_task(trace.TASK, args=[OP, OUTBOX, DELIVERY], queue=queue)
            with app.connection_for_read() as connection:
                with connection.channel() as channel:
                    first = channel.basic_get(queue, no_ack=False)
                    self.assertIsNotNone(first)
                    tag = first.delivery_tag
                    saved = client.hget("unacked", tag)
                    before = unacked_message(tag.encode(), saved, queue)
                    self.assertEqual(before["task_id"], result.id)
                    self.assertFalse(first.delivery_info.get("redelivered", False))
                    restore_unacked(url, tag)
                    self.assertIsNone(client.hget("unacked", tag))
                    self.assertIsNone(client.zscore("unacked_index", tag))
                    second = channel.basic_get(queue, no_ack=False)
                    self.assertIsNotNone(second)
                    self.assertTrue(second.delivery_info["redelivered"])
                    self.assertEqual(second.headers["id"], first.headers["id"])
                    self.assertEqual(second.payload, first.payload)
                    self.assertEqual(second.delivery_tag, tag)
                    second.ack()
            self.assertIsNone(client.hget("unacked", tag))
        finally:
            try:
                if tag is not None:
                    client.hdel("unacked", tag)
                    client.zrem("unacked_index", tag)
                client.delete(queue, "_kombu.binding." + queue)
            finally:
                app.close()
                client.close()

    def test_actual_celery_publish_envelope_round_trip_and_body_signals(self):
        from celery import Celery
        app = Celery("broker-observer-test", broker="memory://", set_as_current=False)
        queue = "safeact-test-" + uuid4().hex
        app.conf.update(task_default_queue=queue, task_ignore_result=True, accept_content=["json"])
        def execute_ids(*args):
            return "done"
        task = app.task(name=trace.TASK, shared=False)(execute_ids)
        connections = []
        try:
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SAFEACT_STACK_BROKER_TRACE_DIR": directory}):
                connections += trace.install(app, SCHEMA, queue, role="publisher")
                connections += trace.install(app, SCHEMA, queue, role="worker")
                result = app.send_task(trace.TASK, args=[OP, OUTBOX, DELIVERY], queue=queue)
                with app.connection_for_read() as connection:
                    with connection.channel() as channel:
                        message = channel._get(queue)
                decoded = trace.queued_message(json.dumps(message).encode(), queue)
                self.assertEqual(decoded["task_id"], result.id)
                task.apply(args=[OP, OUTBOX, DELIVERY], task_id=result.id, throw=True)
                rows = trace.read_trace(directory, 0)
                self.assertEqual([r["event"] for r in rows],
                    ["publish_started", "publish_returned", "task_started", "task_returned"])
                self.assertTrue(all(r["operation_id"] == OP for r in rows))
                self.assertFalse(any(r["event"] == "task_received" for r in rows))
        finally:
            for sig, callback in connections:
                sig.disconnect(callback)
            app.close()
