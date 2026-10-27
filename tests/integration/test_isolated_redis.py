"""Explicit opt-in: an owned Redis container, its real kill, and stack credentials."""

import os
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


ENABLED = (os.environ.get("SAFEACT_RUN_BROKER_TESTS") == "1"
           and os.environ.get("SAFEACT_RUN_ISOLATED_REDIS_TESTS") == "1")


@unittest.skipUnless(ENABLED, "explicit isolated Redis integration opt-in")
class IsolatedRedisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import redis
        from chaos.isolated_redis import IsolatedRedis
        from storage.database import local_redis_url

        cls.shared = redis.Redis.from_url(local_redis_url(9), socket_connect_timeout=2, socket_timeout=2)
        cls.addClassCleanup(cls.shared.close)
        cls.shared_run = cls.shared.info("server")["run_id"]
        cls.broker = IsolatedRedis(6)
        cls.addClassCleanup(cls.broker.stop)
        cls.broker.start()
        cls.client = redis.Redis.from_url(cls.broker.url, socket_connect_timeout=2, socket_timeout=2)
        cls.addClassCleanup(cls.client.close)

    def test_kill_removes_real_queued_celery_message_and_keeps_shared_server(self):
        from celery import Celery
        from deploy.stack.broker_trace import TASK, queued_message

        queue = "safeact-q6-" + uuid4().hex[:6]
        args = [str(uuid4()) for _ in range(3)]
        app = Celery("isolated-kill-protocol", broker=self.broker.url, set_as_current=False)
        app.conf.update(task_default_queue=queue, task_serializer="json", accept_content=["json"], task_ignore_result=True)
        try:
            first = app.send_task(TASK, args=args, queue=queue)
            before = self.client.lrange(queue, 0, -1)
            self.assertEqual(len(before), 1)
            message = queued_message(before[0], queue)
            self.assertEqual(message["task_id"], first.id)
            self.broker.crash_recover(self.client, queue, before, [message], lambda *a, **k: None)
            self.assertEqual(self.client.dbsize(), 0)
            second = app.send_task(TASK, args=args, queue=queue)
            self.assertNotEqual(first.id, second.id)
            with app.connection_for_read() as connection:
                with connection.channel() as channel:
                    delivered = channel.basic_get(queue, no_ack=False)
                    self.assertIsNotNone(delivered)
                    self.assertEqual(delivered.headers["id"], second.id)
                    self.assertEqual(delivered.payload[0], args)
                    delivered.ack()
            self.assertEqual(self.shared.info("server")["run_id"], self.shared_run)
        finally:
            app.close()

    def test_private_credentials_reach_stack_configuration_and_cannot_override_demo(self):
        import redis
        from deploy.demo.bootstrap import write_credentials
        from deploy.stack.config import load

        values = dict(SAFEACT_DEMO_SCHEMA="safeact_it_" + uuid4().hex, **self.broker.credentials())
        for component in ("API", "WORKER", "RECOVERER", "ADVANCER", "MCP"):
            values["SAFEACT_DEMO_" + component + "_ROLE"] = "fixture_" + component.lower()
            values["SAFEACT_DEMO_" + component + "_PASSWORD"] = "test-only"
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "SAFEACT_STACK_REDIS_DB": "8", "SAFEACT_STACK_QUEUE": "safeact-q6-abcdef"}):
            path = Path(directory) / "credentials"
            write_credentials(path, values)
            config = load(path)
            self.assertEqual(config.redis_url, self.broker.url)
            self.assertNotIn(self.broker._password, repr(config))
            client = redis.Redis.from_url(config.redis_url, socket_connect_timeout=2, socket_timeout=2)
            try:
                self.assertTrue(client.ping())
            finally:
                client.close()
            values["SAFEACT_DEMO_SCHEMA"] = "safeact_demo"
            write_credentials(path, values)
            with self.assertRaises(RuntimeError):
                load(path)
        self.assertEqual(self.shared.info("server")["run_id"], self.shared_run)
