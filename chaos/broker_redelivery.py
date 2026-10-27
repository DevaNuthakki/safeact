"""Force early transport restoration of a real unacked message, without republishing.

The original provider call stays held until a second worker body returns.
This tests premature redelivery, not expiry of the normal visibility timeout.
Kombu's pinned Redis QoS.restore_by_tag performs the actual restoration.
"""

import json
import re
import time

from uuid import UUID

from chaos import SLOT_REDIS_DBS
from chaos.broker_protocol import protocol as select_protocol
from deploy.stack.broker_trace import IDS, queued_message


def unacked_message(tag, value, queue, *, protocol='safeact'):
    if not isinstance(tag, bytes) or str(UUID(tag.decode())) != tag.decode():
        raise ValueError("Invalid unacked delivery tag")
    if not isinstance(value, bytes) or len(value) > 131072:
        raise ValueError("Invalid unacked envelope")
    data = json.loads(value)
    if not isinstance(data, list) or len(data) != 3 or data[2] != queue:
        raise ValueError("Unexpected unacked routing")
    message, exchange, routing_key = data
    if message["properties"]["delivery_tag"] != tag.decode():
        raise ValueError("Unacked delivery tag mismatch")
    # This read-only transport decoder predates the reserved qualification
    # queues. Keep its SafeAct API usable by ordinary broker tests; privileged
    # fault drivers validate their owned queue and DB in their constructors.
    if protocol == 'safeact':
        return queued_message(json.dumps(message).encode(), queue)
    return select_protocol(protocol).decode(json.dumps(message).encode(), queue)


def restore_unacked(broker_url, tag):
    from importlib.metadata import version
    from kombu import Connection
    if version("kombu") != "5.5.4":
        raise RuntimeError("Redis restoration requires the reviewed Kombu 5.5.4 protocol")
    with Connection(broker_url, connect_timeout=2, transport_options={
            "socket_timeout": 2, "socket_connect_timeout": 2}) as connection:
        with connection.channel() as channel:
            if channel.connection.driver_type != "redis":
                raise RuntimeError("Redelivery requires the Redis transport")
            channel.qos.restore_by_tag(tag)


class DispatchRedelivery:
    def __init__(self, client, redis_db, queue, admitted, dispatching, observations, restore, emit, *, protocol='safeact',expect_refusal=True):
        self.protocol=select_protocol(protocol)
        if type(expect_refusal) is not bool or (not expect_refusal and protocol!='baseline'):raise ValueError('Weak delivery behavior requires an explicit baseline protocol')
        self.expect_refusal=expect_refusal
        if (type(redis_db) is not int or redis_db not in SLOT_REDIS_DBS
                or client.connection_pool.connection_kwargs.get("db") != redis_db
                or not self.protocol.valid_queue(queue)):
            raise RuntimeError("Redelivery requires the locked slot's private Redis database and queue")
        self.client, self.redis_db, self.queue = client, redis_db, queue
        self.admitted, self.dispatching, self.observations = admitted, dispatching, observations
        self.restore, self.emit = restore, emit

    def _select(self):
        started = time.monotonic_ns()
        unacked = self.client.hgetall("unacked")
        finished = time.monotonic_ns()
        if len(unacked) > 1000:
            raise RuntimeError("Unacked broker inventory exceeds its bound")
        rows = self.observations()
        admitted = self.admitted()
        candidates = []
        for tag, value in unacked.items():
            message = unacked_message(tag, value, self.queue,protocol=self.protocol.name)
            if message["operation_id"] not in admitted:
                continue
            mine = [e for e in rows if all(e.get(k) == message[k] for k in IDS)]
            bodies = [e for e in mine if e["event"] == "task_started" and not e["redelivered"]
                      and not any(r["event"] == "task_returned" and r["pid"] == e["pid"] for r in mine)]
            if (len(bodies) == 1 and bodies[0]["monotonic_ns"] < started
                    and any(e["event"] == "publish_returned" and e["monotonic_ns"] < started for e in mine)):
                candidates.append((tag, value, message, bodies[0]))
        if len(candidates) > 1:
            raise RuntimeError("Ambiguous original running broker message")
        if not candidates:
            return None
        tag, value, message, body = candidates[0]
        self.emit("broker_unacked_observed", source="broker_store", **message,
            redis_db=self.redis_db, queue=self.queue, delivery_tag=tag.decode(),
            sample_started_ns=started, sample_finished_ns=finished)
        return tag, value, message, body

    def run(self, held, cancelled):
        try:
            while not cancelled.is_set():
                if held.started.is_set():
                    selected = self._select()
                    if selected is not None:
                        break
                cancelled.wait(0.01)
            else:
                raise RuntimeError("Redelivery cancelled before a held scenario message was running")
            tag, value, message, original = selected
            # This separate DB connection must observe the persisted fence while
            # the provider is held. Never use the task's reported state as proof.
            self.dispatching(message)
            if self.client.hget("unacked", tag) != value:
                raise RuntimeError("Original unacknowledged broker entry changed before restoration")
            details = dict(message, source="broker_transport", redis_db=self.redis_db,
                queue=self.queue, delivery_tag=tag.decode(), method="QoS.restore_by_tag")
            self.emit("broker_restore_requested", **details)
            self.restore(tag.decode())
            self.emit("broker_restore_returned", **details)
            deadline = time.monotonic() + 4.0
            while not cancelled.is_set() and time.monotonic() < deadline:
                rows = [e for e in self.observations()
                        if all(e.get(k) == message[k] for k in IDS) and e["redelivered"]]
                ends = [e for e in rows if e["event"] == "task_returned"
                        and e["pid"] != original["pid"]]
                if ends:
                    end = ends[0]
                    if not any(e["event"] == "task_started" and e["pid"] == end["pid"]
                               and e["monotonic_ns"] < end["monotonic_ns"] for e in rows):
                        raise RuntimeError("Redelivered task return lacks its body-start observation")
                    self.dispatching(message)
                    if self.expect_refusal and end.get("task_status") != "not_claimable":
                        raise RuntimeError("The concurrent redelivery was not refused at claim")
                    if not self.expect_refusal and end.get('task_status') not in ('recorded','not_claimable'):
                        raise RuntimeError('Weak queue redelivery did not report its actual execution result')
                    self.emit("broker_redelivery_finished", **message, duplicate_pid=end["pid"])
                    return
                cancelled.wait(0.01)
            raise RuntimeError("Concurrent redelivery did not finish while the original request was held")
        finally:
            held.release()
