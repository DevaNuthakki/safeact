"""Celery over Redis behind the relay's Publisher port (experimental).

The broker is coordination only. PostgreSQL holds the durable intent,
outbox and lifecycle; a task message carries three ids and no
authority: the worker claims from the database and dispatches only
what the durable record says. So:

- lost messages (Redis flushed, restarted without AOF, or crashed
  between fsyncs) lose nothing: the relay republishes every
  never-claimed or reclaimable operation on its next pass (F08);
- duplicate or redelivered messages are harmless: the claim admits one
  owner, every other delivery is NOT_CLAIMABLE (F04, F08).

Broker settings (checked against Celery 5.5 / kombu 5.5):

- task_acks_late: a message is acknowledged after the task body
  returns, so a worker lost mid-task leaves it unacknowledged and it is
  redelivered rather than silently dropped;
- task_reject_on_worker_lost: a task whose worker process dies is
  requeued (Celery's default would acknowledge it);
- task_acks_on_failure_or_timeout (default True, kept): a task that
  raises is acknowledged, not redelivered in a loop; the worker never
  raises for lifecycle outcomes, and anything left behind is found by
  the reconciler from the database;
- worker_prefetch_multiplier=1: a worker holds at most one unacked
  message, so a crash strands as little as possible;
- broker_transport_options.visibility_timeout: how long an unacked
  message stays invisible before Redis hands it out again. It must
  exceed the longest task (claim lease plus request deadline), or a
  slow task is redelivered while still running; that duplicate is
  harmless (the claim refuses it) but wasteful;
- JSON only, no result backend: tasks return nothing of value.
"""

from uuid import UUID

from celery import Celery

from storage.experimental_durable_execution import ExecutionTask


TASK_NAME = "safeact.execute_operation"


def broker_settings(*, queue, visibility_timeout):
    if type(visibility_timeout) is not int or visibility_timeout < 1:
        raise ValueError("visibility timeout must be whole seconds")
    return {
        "task_acks_late": True,
        "task_reject_on_worker_lost": True,
        "task_acks_on_failure_or_timeout": True,
        "worker_prefetch_multiplier": 1,
        "task_serializer": "json",
        "accept_content": ["json"],
        "task_ignore_result": True,
        "result_backend": None,
        "task_default_queue": queue,
        "broker_connection_retry_on_startup": True,
        "broker_transport_options": {
            "visibility_timeout": visibility_timeout,
        },
        "worker_hijack_root_logger": False,
    }


def make_app(broker_url, *, queue, visibility_timeout=3600):
    """A Celery app whose single task runs one durable execution.

    The worker process sets ``app.safeact_execute`` to a callable taking
    an ExecutionTask (normally ExecutionWorker.execute).
    """

    app = Celery("safeact", broker=broker_url, set_as_current=False)
    app.conf.update(
        broker_settings(queue=queue, visibility_timeout=visibility_timeout)
    )

    @app.task(name=TASK_NAME, bind=True, shared=False)
    def execute_operation(self, operation_id, outbox_id, delivery_id):
        result = self.app.safeact_execute(ExecutionTask(
            UUID(operation_id), UUID(outbox_id), UUID(delivery_id),
        ))
        return result.status.value

    return app


class CeleryPublisher:
    """Publisher port: one message per relayed outbox delivery."""

    def __init__(self, app, *, queue):
        self._app = app
        self._queue = queue
        self.published = 0

    def publish(self, task):
        self._app.send_task(
            TASK_NAME,
            args=[
                str(task.operation_id), str(task.outbox_id),
                str(task.delivery_id),
            ],
            queue=self._queue,
        )
        self.published += 1
