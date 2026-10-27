"""Throwaway-database harness for Alembic round trips (0004 onward).

Creates a ``safeact_it_<hex32>`` database on the local dev server and
drops it afterwards; the dev database is untouched. Not a test module.
"""

import re
import unittest

from pathlib import Path
from uuid import uuid4

import psycopg
import sqlalchemy as sa

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from psycopg import sql
from sqlalchemy.pool import NullPool

from storage.database import local_database_url
from storage.schema import metadata
from tests.integration.test_postgres_smoke import database_settings


ROOT = Path(__file__).resolve().parents[2]


class PostgreSQLXid8(sa.types.UserDefinedType):
    """Native xid8 representation for disposable-database reflection."""

    cache_ok = True

    def get_col_spec(self, **kw):
        return "xid8"


class MigrationRoundTripCase(unittest.TestCase):

    upgrade = staticmethod(command.upgrade)
    downgrade = staticmethod(command.downgrade)

    def setUp(self):
        self.name = "safeact_it_" + uuid4().hex
        if not re.fullmatch(r"safeact_it_[0-9a-f]{32}", self.name):
            raise RuntimeError("STOP: invalid disposable database")

        settings = database_settings()
        if settings["host"] != "127.0.0.1":
            raise RuntimeError("STOP: local database required")

        self.admin = psycopg.connect(**settings, autocommit=True)
        self.addCleanup(self.admin.close)

        self.admin.execute(sql.SQL("CREATE DATABASE {}").format(
            sql.Identifier(self.name)
        ))
        self.addCleanup(self.drop_database)

        self.engine = sa.create_engine(
            local_database_url().set(database=self.name),
            poolclass=NullPool,
            connect_args={"connect_timeout": 5},
        )
        self.addCleanup(self.engine.dispose)
        # Copy the registry for this test engine; retain native type identity.
        self.engine.dialect.ischema_names = dict(self.engine.dialect.ischema_names)
        self.engine.dialect.ischema_names.setdefault("xid8", PostgreSQLXid8)

        self.config = Config(str(ROOT / "alembic.ini"))
        self.config.set_main_option(
            "script_location", str(ROOT / "deploy" / "migrations")
        )

    def drop_database(self):
        self.engine.dispose()
        self.admin.execute(sql.SQL(
            "DROP DATABASE IF EXISTS {} WITH (FORCE)"
        ).format(sql.Identifier(self.name)))

    def migrate(self, action, revision):
        with self.engine.begin() as connection:
            self.config.attributes["connection"] = connection
            try:
                action(self.config, revision)
            finally:
                self.config.attributes.pop("connection", None)

    def query(self, statement, params=None):
        with self.engine.connect() as connection:
            return connection.execute(
                sa.text(statement), params or {}
            ).fetchall()

    def execute(self, statement, params=None):
        with self.engine.begin() as connection:
            connection.execute(sa.text(statement), params or {})

    def version(self):
        return self.query("SELECT version_num FROM alembic_version")

    def functions(self):
        """name -> (definition, owner, acl) for every public function."""

        return {
            name: (definition, owner, acl)
            for name, definition, owner, acl in self.query("""
                SELECT p.proname, pg_get_functiondef(p.oid),
                       pg_get_userbyid(p.proowner), p.proacl::text
                FROM pg_proc p
                WHERE p.pronamespace = 'public'::regnamespace
                  AND p.proname LIKE 'safeact\\_%'
            """)
        }

    def relations(self):
        return {
            name for (name,) in self.query("""
                SELECT relname FROM pg_class
                WHERE relnamespace = 'public'::regnamespace
                  AND relkind IN ('r', 'v')
            """)
        }

    def removals(self, include_object=None):
        options = {"compare_type": True}
        if include_object is not None:
            options["include_object"] = include_object

        with self.engine.connect() as connection:
            diffs = compare_metadata(
                MigrationContext.configure(connection, opts=options),
                metadata,
            )

        flat = []
        for diff in diffs:
            flat.extend(diff if isinstance(diff, list) else [diff])

        return {
            (diff[0], diff[1].name)
            for diff in flat
            if diff[0] in ("remove_table", "remove_index")
        }
