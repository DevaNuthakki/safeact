"""Pool policy boundaries without opening database connections."""
import os
import unittest
from unittest import mock

import psycopg
from sqlalchemy.exc import TimeoutError as PoolTimeout

from storage.connection_pool import ConnectionPool


class ConnectionPoolPolicyTests(unittest.TestCase):
    def pool(self, **kwargs):
        pool = ConnectionPool(dict(user="synthetic_worker", password="private-test-value"), **kwargs)
        self.addCleanup(pool.close)
        return pool

    def test_pool_is_lazy_and_repr_does_not_expose_credentials(self):
        with mock.patch("storage.connection_pool.psycopg.connect") as connect:
            pool = self.pool()
            self.assertNotIn("private-test-value", repr(pool))
            pool.close()
            connect.assert_not_called()

    def test_capacity_and_wait_must_be_bounded(self):
        for kwargs in (dict(size=True), dict(size=0), dict(size=17),
                       dict(timeout=True), dict(timeout=0), dict(timeout=61),
                       dict(timeout=float("inf")), dict(timeout=float("nan"))):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.pool(**kwargs)

    def test_role_and_transactions_are_explicit(self):
        for settings in ({}, dict(user=""), dict(user="worker", autocommit=True)):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                ConnectionPool(settings)

    def test_forked_owner_is_refused_before_pool_checkout(self):
        pool = self.pool()
        with mock.patch("storage.connection_pool.os.getpid", return_value=os.getpid()+1), \
                mock.patch.object(pool._pool, "connect") as connect:
            with self.assertRaises(RuntimeError), pool.connection():
                self.fail("Foreign owner was accepted")
            pool.close()  # must not close the parent's connections
            connect.assert_not_called()
        self.assertFalse(pool._closed)

    def test_closed_pool_never_connects_and_close_is_idempotent(self):
        pool = self.pool()
        pool.close()
        pool.close()
        with mock.patch.object(pool._pool, "connect") as connect:
            with self.assertRaises(psycopg.OperationalError), pool.connection():
                self.fail("Closed pool was accepted")
            connect.assert_not_called()

    def test_capacity_error_is_sanitized_and_never_calls_business_code(self):
        pool = self.pool()
        with mock.patch.object(pool._pool, "connect", side_effect=PoolTimeout("private-test-value")):
            with self.assertRaisesRegex(psycopg.OperationalError, "^Database pool is busy$"):
                with pool.connection():
                    self.fail("Exhausted pool accepted work")
