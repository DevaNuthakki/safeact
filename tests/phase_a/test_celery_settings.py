"""Celery broker settings contract. No broker contacted."""

import unittest
from types import SimpleNamespace

from storage.experimental_celery import (
    TASK_NAME,
    broker_settings,
    make_app,
)


class CelerySettingsTests(unittest.TestCase):

    def test_redelivery_and_loss_settings(self):
        settings = broker_settings(queue="q", visibility_timeout=900)
        self.assertTrue(settings["task_acks_late"])
        self.assertTrue(settings["task_reject_on_worker_lost"])
        self.assertTrue(settings["task_acks_on_failure_or_timeout"])
        self.assertEqual(settings["worker_prefetch_multiplier"], 1)
        self.assertEqual(
            settings["broker_transport_options"], {"visibility_timeout": 900}
        )
        self.assertEqual(settings["accept_content"], ["json"])
        self.assertIsNone(settings["result_backend"])

    def test_visibility_timeout_must_be_whole_seconds(self):
        for bad in (0, -1, 1.5, "60"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    broker_settings(queue="q", visibility_timeout=bad)

    def test_app_registers_only_the_execution_task(self):
        app = make_app("memory://", queue="q", visibility_timeout=60)
        names = {n for n in app.tasks if not n.startswith("celery.")}
        self.assertEqual(names, {TASK_NAME})

    def test_safeact_and_baseline_factories_do_not_share_task_registration(self):
        from chaos.baselines.processes import TASK_NAME as BASELINE_TASK, celery_app
        for baseline_first in (False,True):
            config=SimpleNamespace(broker="memory://",queue="baseline-unit-queue")
            factories=(lambda:make_app("memory://",queue="safeact-unit-queue"),lambda:celery_app(config))
            order=(1,0) if baseline_first else (0,1)
            apps={i:factories[i]() for i in order}
            try:
                self.assertEqual({n for n in apps[0].tasks if not n.startswith("celery.")},{TASK_NAME})
                self.assertEqual({n for n in apps[1].tasks if not n.startswith("celery.")},{BASELINE_TASK})
            finally:
                for app in apps.values():app.close()


if __name__ == "__main__":
    unittest.main()
