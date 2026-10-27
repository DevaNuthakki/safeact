"""Explicit opt-in PostgreSQL integration tests.

No schema or application data is modified by these tests.
"""

from pathlib import Path
import os
import unittest

import psycopg
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL


def database_settings():
    root = Path(__file__).resolve().parents[2]
    path = root / ".env"

    if not path.is_file():
        raise RuntimeError(
            "SafeAct local database configuration is missing"
        )

    if path.stat().st_mode & 0o077:
        raise RuntimeError(
            "Database credentials have unsafe file permissions"
        )

    values = {}

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        key, separator, value = line.partition("=")

        if not separator or not key:
            raise RuntimeError("Malformed local configuration")

        if key in values:
            raise RuntimeError("Duplicate local configuration key")

        values[key] = value

    password = values.get("SAFEACT_DEV_DB_PASSWORD")
    port = values.get("SAFEACT_DEV_DB_PORT")

    if not password or not port:
        raise RuntimeError("Incomplete database configuration")

    port_number = int(port)

    if not 1 <= port_number <= 65535:
        raise RuntimeError("Invalid database port")

    return {
        "host": "127.0.0.1",
        "port": port_number,
        "dbname": "safeact_dev",
        "user": "safeact_dev",
        "password": password,
        "connect_timeout": 5,
    }


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1",
    "Database integration tests require explicit opt-in",
)
class PostgreSQLSmokeTests(unittest.TestCase):

    def test_psycopg_authenticated_connection(self):
        settings = database_settings()

        with psycopg.connect(**settings) as connection:
            row = connection.execute(
                """
                SELECT
                    current_database(),
                    current_user,
                    current_setting('server_version_num')
                """
            ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], "safeact_dev")
        self.assertEqual(row[1], "safeact_dev")
        self.assertTrue(
            170000 <= int(row[2]) < 180000,
            "Expected PostgreSQL major version 17",
        )

    def test_sqlalchemy_connection(self):
        settings = database_settings()

        url = URL.create(
            "postgresql+psycopg",
            username=settings["user"],
            password=settings["password"],
            host=settings["host"],
            port=settings["port"],
            database=settings["dbname"],
        )

        engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 5},
        )

        try:
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT current_database(), current_user"
                    )
                ).one()

            self.assertEqual(
                tuple(row),
                ("safeact_dev", "safeact_dev"),
            )
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
