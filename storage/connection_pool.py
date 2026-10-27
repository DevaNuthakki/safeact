"""Bounded, process-owned leases of native psycopg connections.

Pool cleanup is resource hygiene, never evidence about an uncertain business
commit. There is no retry of application work in this module.
"""

import atexit
import logging
import os
import threading
from contextlib import contextmanager

import psycopg
from sqlalchemy import event
from psycopg.rows import tuple_row
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.pool import QueuePool


log = logging.getLogger("safeact.database")

# Preserve server-side prepared statements and their driver's cache. All
# application SQL names its protected schema explicitly. RESET ALL restores
# startup/role settings, including the API's statement timeout.
RESET_SESSION = (
    "CLOSE ALL; RESET ROLE; SET SESSION AUTHORIZATION DEFAULT; RESET ALL; "
    "UNLISTEN *; SELECT pg_advisory_unlock_all(); DISCARD TEMP; DISCARD SEQUENCES"
)


class ConnectionPool:
    def __init__(self, settings, *, size=4, timeout=5):
        if type(size) is not int or not 1 <= size <= 16:
            raise ValueError("Pool size must be 1..16")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
            raise ValueError("Pool timeout must be positive and bounded")
        if not isinstance(settings.get("user"), str) or not settings["user"]:
            raise ValueError("An authenticated pool role is required")
        self._settings = dict(settings)
        if self._settings.get("autocommit", False):
            raise ValueError("Pooled application connections must use transactions")
        self._role = self._settings["user"]
        self._pid = os.getpid()
        self._closed = False
        self._lock = threading.Lock()
        self._pool = QueuePool(self._create, pool_size=size, max_overflow=0,
                               timeout=timeout, reset_on_return=None)
        atexit.register(self.close)

    def __repr__(self):
        return f"ConnectionPool(role={self._role!r}, pid={self._pid})"

    def _check_process(self):
        if os.getpid() != self._pid:
            raise RuntimeError("Database pool cannot cross a process boundary")

    def _create(self):
        self._check_process()
        with self._lock:
            if self._closed:
                raise psycopg.OperationalError("Database pool is closed")
            return psycopg.connect(**self._settings)

    def _healthy(self, connection):
        if type(connection) is not psycopg.Connection or connection.closed:
            return False
        if connection.info.user != self._role:
            return False
        if connection.info.transaction_status is not psycopg.pq.TransactionStatus.IDLE:
            return False
        # Detect server-terminated idle sockets before handing them to business
        # code. This is a read-only liveness probe, not an application retry.
        # Rolling back a probe transaction clears Psycopg's prepared cache.
        # Probe in autocommit while no application transaction is open.
        connection.autocommit = True
        try:
            connection.execute("SELECT 1", prepare=False)
        finally:
            if not connection.closed:
                connection.autocommit = False
        return True

    @staticmethod
    def _reset(connection):
        connection.rollback()
        connection.autocommit = False
        connection.isolation_level = None
        connection.read_only = None
        connection.deferrable = None
        connection.row_factory = tuple_row
        connection.cursor_factory = psycopg.Cursor
        connection.execute(RESET_SESSION, prepare=False)
        connection.commit()

    @contextmanager
    def connection(self):
        self._check_process()
        with self._lock:
            if self._closed:
                raise psycopg.OperationalError("Database pool is closed")
        lease = None
        # A dead socket is replaced once. No business call has begun here.
        for _ in range(2):
            try:
                lease = self._pool.connect()
            except PoolTimeout:
                raise psycopg.OperationalError("Database pool is busy") from None
            raw = lease.driver_connection
            try:
                with self._lock:
                    if self._closed:
                        raise psycopg.OperationalError("Database pool is closed")
                if not self._healthy(raw):
                    raise psycopg.OperationalError("Database connection is unavailable")
                break
            except (psycopg.Error, OSError):
                lease.invalidate()
                lease.close()
                lease = None
        if lease is None:
            raise psycopg.OperationalError("Database connection is unavailable")
        try:
            yield raw
        finally:
            try:
                if raw.closed or self._closed:
                    lease.invalidate()
                else:
                    try:
                        self._reset(raw)
                    except Exception as error:
                        # Never reuse uncertain session state or mask the
                        # business exception/result with cleanup diagnostics.
                        lease.invalidate()
                        log.warning("database_connection_discarded", extra={
                            "error_type": type(error).__name__,
                        })
            finally:
                # Serialize return with shutdown. If close() disposed the
                # idle queue while reset was in progress, this connection
                # must be discarded rather than returned after disposal.
                with self._lock:
                    if self._closed and lease.is_valid:
                        lease.invalidate()
                    lease.close()

    def close(self):
        # A forked child must not send protocol messages on parent sockets.
        if os.getpid() != self._pid:
            return
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._pool.dispose()
        atexit.unregister(self.close)


def reset_engine_sessions(engine):
    """Use the same session hygiene for an internally owned SQLAlchemy engine."""
    def reset(connection, _record, state):
        if not state.terminate_only:
            ConnectionPool._reset(connection)
    event.listen(engine, "reset", reset)
    return engine
