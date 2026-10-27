"""Prove queued loss by flush or by killing a separately owned Redis server."""

import os
import re
import signal
import time

from chaos import SLOT_REDIS_DBS
from chaos.broker_protocol import protocol as select_protocol
from deploy.stack.broker_trace import IDS, queued_message


class QueuedLoss:
    def __init__(self, stack, client, redis_db, queue, admitted, unclaimed, observations, emit, *, protocol='safeact'):
        self.protocol=select_protocol(protocol)
        if (redis_db not in SLOT_REDIS_DBS or type(redis_db) is not int
                or client.connection_pool.connection_kwargs.get("db") != redis_db
                or not self.protocol.valid_queue(queue)):
            raise RuntimeError("Broker loss requires the slot's private Redis database and queue")
        self.stack, self.client, self.redis_db, self.queue = stack, client, redis_db, queue
        self.admitted, self.unclaimed, self.observations, self.emit = admitted, unclaimed, observations, emit
        self.paused = None

    def begin(self):
        process = self.stack.processes["worker"]
        if (type(process.pid) is not int or process.pid <= 1 or process.pid == os.getpgrp()
                or process.poll() is not None or os.getpgid(process.pid) != process.pid):
            raise RuntimeError("Broker consumer is not this stack's live process group")
        self.stack.kill("worker", signal.SIGKILL)
        self.stack.processes[f"killed-worker-{process.pid}"] = process
        if process.returncode != -signal.SIGKILL:
            raise RuntimeError("Broker consumer did not exit by SIGKILL")
        self.emit("broker_consumer_stopped", worker_pid=process.pid, returncode=process.returncode)
        # A SIGSTOP would leave its outstanding BRPOP active on the server.
        # Require those connections to close before any scenario admission.
        deadline = time.monotonic() + 3
        while any(int(c.get("db", -1)) == self.redis_db and "b" in c.get("flags", "")
                  for c in self.client.client_list()):
            if time.monotonic() >= deadline:
                raise RuntimeError("An old broker consumer is still blocked in this slot")
            time.sleep(0.02)
        self.emit("broker_consumers_disconnected", redis_db=self.redis_db)

    def _pause_relay(self):
        process = self.stack.processes["relay"]
        if (type(process.pid) is not int or process.pid <= 1 or process.pid == os.getpgrp()
                or process.poll() is not None or os.getpgid(process.pid) != process.pid):
            raise RuntimeError("Relay is not this stack's live process group")
        self.paused = process
        os.killpg(process.pid, signal.SIGSTOP)
        deadline = time.monotonic() + 2
        while True:
            pid, status = os.waitpid(process.pid, os.WNOHANG | os.WUNTRACED)
            if pid:
                if os.WIFSTOPPED(status) and os.WSTOPSIG(status) == signal.SIGSTOP:
                    self.emit("broker_relay_stopped", relay_pid=process.pid,
                        requested_signal=int(signal.SIGSTOP), stop_signal=int(os.WSTOPSIG(status)), wait_status=status)
                    return
                if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                    process.returncode = os.waitstatus_to_exitcode(status)
                raise RuntimeError("Relay did not report SIGSTOP")
            if time.monotonic() >= deadline:
                raise RuntimeError("Relay stop was not observed before the deadline")
            time.sleep(0.01)

    def resume(self):
        if self.paused is not None:
            process = self.paused
            try:
                os.killpg(process.pid, signal.SIGCONT)
            except ProcessLookupError:
                self.paused = None
                return
            self.paused = None
            self.emit("broker_relay_continue_requested", relay_pid=process.pid)

    def finish(self, cancelled):
        try:
            while not cancelled.is_set():
                admitted = self.admitted()
                messages = [self.protocol.decode(row, self.queue) for row in self.client.lrange(self.queue, 0, -1)]
                published = {tuple(row[key] for key in IDS) for row in self.observations()
                             if row["event"] == "publish_returned"}
                if any(m["operation_id"] in admitted and tuple(m[key] for key in IDS) in published for m in messages):
                    break
                cancelled.wait(0.02)
            else:
                raise RuntimeError("Broker loss cancelled before a scenario message was queued")
            self._pause_relay()
            self._remove()
        finally:
            try:
                self.resume()
            finally:
                replacement = self.stack.spawn("worker")
                self.emit("broker_consumer_restarted", worker_pid=replacement.pid)

    def _remove(self):
        with self.client.pipeline() as pipe:
            pipe.watch(self.queue)
            before = pipe.lrange(self.queue, 0, -1)
            if not 0 < len(before) <= 1000:
                raise RuntimeError("Expected a bounded, nonempty broker queue")
            messages = [self.protocol.decode(row, self.queue) for row in before]
            target = [m for m in messages if m["operation_id"] in self.admitted()]
            if not target:
                raise RuntimeError("No admitted scenario message is queued")
            self.unclaimed(target)
            pipe.multi()
            pipe.lrange(self.queue, 0, -1)
            pipe.execute_command("FLUSHDB", "SYNC")
            pipe.llen(self.queue)
            pipe.dbsize()
            started = time.monotonic_ns()
            captured, acknowledged, after_count, database_size = pipe.execute()
            finished = time.monotonic_ns()
            if captured != before or acknowledged is not True or after_count != 0 or database_size != 0:
                raise RuntimeError("Atomic broker removal was not verified")
            self.emit("broker_messages_flushed", source="broker_store", redis_db=self.redis_db,
                queue=self.queue, messages=messages, before_count=len(captured), after_count=after_count,
                database_size_after=database_size, acknowledged=True,
                sample_started_ns=started, sample_finished_ns=finished)


class CrashedBrokerLoss(QueuedLoss):
    """Same queued/unclaimed/republication proof, with an owned process kill."""
    def __init__(self, *args, broker=None, **kwargs):
        from chaos.isolated_redis import IsolatedRedis
        if not isinstance(broker, IsolatedRedis):
            raise RuntimeError("Redis kill requires a newly owned isolated broker")
        super().__init__(*args, **kwargs)
        if broker.redis_db != self.redis_db:
            raise RuntimeError("Isolated broker does not belong to this slot")
        self.broker = broker

    def _remove(self):
        before = self.client.lrange(self.queue, 0, -1)
        if not 0 < len(before) <= 1000:
            raise RuntimeError("Expected a bounded, nonempty private broker queue")
        messages = [self.protocol.decode(row, self.queue) for row in before]
        targets = [m for m in messages if m["operation_id"] in self.admitted()]
        if not targets:
            raise RuntimeError("No admitted scenario message is queued on the private broker")
        self.unclaimed(targets)
        options={} if self.protocol.name=='safeact' else dict(protocol=self.protocol.name)
        self.broker.crash_recover(self.client, self.queue, before, messages, self.emit,**options)
