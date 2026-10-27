"""Owned disposable B3 database and private process configuration (harness).

The application role owns no schema and has no SafeAct privileges. Only the
administrator used by the harness can create/drop this fresh environment.
"""

import json
import os
import secrets
from pathlib import Path
from uuid import uuid4

import psycopg
import sqlalchemy as sa
from psycopg import sql
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from chaos.baselines.store import Tables
from chaos.baselines.identity import NO_SECRET_LOGGING
from storage.database import local_admin_settings, local_database_url


class Environment:
    def __init__(self):
        suffix = uuid4().hex
        self.t = Tables("safeact_baseline_it_"+suffix)
        self.owner = "safeact_base_"+suffix+"_owner"
        self.role = "safeact_base_"+suffix+"_app"
        self.password = secrets.token_urlsafe(32)
        self.created = False
        self.engine = None

    def __repr__(self):
        return f"BaselineEnvironment(schema={self.t.schema!r})"

    def start(self):
        admin = sa.create_engine(local_database_url(),poolclass=NullPool)
        try:
            with admin.begin() as c:
                # Password-bearing role DDL must not enter database logs.
                raw = c.connection.driver_connection
                for option,value in NO_SECRET_LOGGING.items():
                    raw.execute(sql.SQL("SET LOCAL {} = {}").format(sql.Identifier(option),sql.Literal(value)))
                raw.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(self.owner)))
                raw.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT").format(
                    sql.Identifier(self.role),sql.Literal(self.password)))
                raw.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = '5s'").format(sql.Identifier(self.role)))
                raw.execute(sql.SQL("ALTER ROLE {} SET idle_in_transaction_session_timeout = '60s'").format(sql.Identifier(self.role)))
                for option,value in NO_SECRET_LOGGING.items():
                    raw.execute(sql.SQL("ALTER ROLE {} SET {} = {}").format(
                        sql.Identifier(self.role),sql.Identifier(option),sql.Literal(value)))
                raw.execute(sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(sql.Identifier(self.t.schema),sql.Identifier(self.owner)))
                raw.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(self.owner)))
                self.t.metadata.create_all(c)
                raw.execute(sql.SQL("RESET ROLE"))
                raw.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(self.t.schema),sql.Identifier(self.role)))
                for table in self.t.metadata.sorted_tables:
                    mutable = table.name in (self.t.jobs.name,self.t.outbox.name,self.t.accounts.name,self.t.sessions.name)
                    readonly = table.name in (self.t.humans.name,self.t.policies.name)
                    raw.execute(sql.SQL("GRANT {} ON {}.{} TO {}").format(
                        sql.SQL("SELECT" if readonly else "SELECT, INSERT, UPDATE" if mutable else "SELECT, INSERT"),
                        sql.Identifier(self.t.schema),sql.Identifier(table.name),sql.Identifier(self.role)))
                raw.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(
                    sql.Identifier(self.t.schema),sql.Identifier(self.role)))
            self.created = True
        except Exception as error:
            raise RuntimeError(f"Baseline environment setup failed: {type(error).__name__}") from None
        finally:
            admin.dispose()
        settings = local_admin_settings()
        self.engine = sa.create_engine(URL.create("postgresql+psycopg",username=self.role,password=self.password,
            host=settings["host"],port=settings["port"],database=settings["dbname"]),
            poolclass=NullPool,isolation_level="READ COMMITTED",hide_parameters=True)
        return self

    def write_config(self, path, provider_urls, *, broker_url=None, queue=None, fault_dir=None, registration_dir=None,execution_mode='b3'):
        settings = dict(local_admin_settings(),user=self.role,password=self.password)
        value = dict(schema=self.t.schema,database=settings,providers=provider_urls,
                     broker_url=broker_url,queue=queue,fault_dir=str(fault_dir) if fault_dir else None)
        if registration_dir is not None:value['registration_dir']=str(registration_dir)
        if execution_mode not in ('b1','b2','b3'):raise ValueError('Unknown baseline execution mode')
        if execution_mode!='b3':value['execution_mode']=execution_mode
        fd = os.open(Path(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600)
        with os.fdopen(fd,"w") as stream:
            json.dump(value,stream)

    def provision_human(self, *, actor_id, tenant_id, username, password, roles, enabled=True):
        """Harness provisioning; never reachable from the baseline application."""
        if (not self.created or type(enabled) is not bool or not set(roles)<= {"requester","investigator","reviewer"}
                or not roles or any(type(s) is not str or not 1<=len(s)<=128 or any(c.isspace() for c in s)
                                   for s in (actor_id,tenant_id,username))
                or type(password) is not str or not 12<=len(password.encode())<=72):
            raise ValueError("Valid owned-environment human provisioning required")
        from psycopg.types.json import Jsonb
        self._provision(sql.SQL("""INSERT INTO {}.baseline_humans
            (actor_id,tenant_id,username,password_hash,roles,enabled)
            VALUES (%s,%s,%s,public.crypt(%s,public.gen_salt('bf',12)),%s,%s)
            ON CONFLICT (actor_id) DO UPDATE SET tenant_id=EXCLUDED.tenant_id,username=EXCLUDED.username,
              password_hash=EXCLUDED.password_hash,roles=EXCLUDED.roles,enabled=EXCLUDED.enabled""").format(
                  sql.Identifier(self.t.schema)),(actor_id,tenant_id,username,password,Jsonb(list(roles)),enabled))

    def provision_review_policy(self, connector_id, contract_version, *, required=True):
        if not self.created or type(required) is not bool:
            raise ValueError("Owned environment and explicit policy required")
        from chaos.profiles import profile
        profile(connector_id,contract_version)
        self._provision(sql.SQL("""INSERT INTO {}.baseline_review_policies
            (connector_id,contract_version,requires_retry_review,revision) VALUES (%s,%s,%s,1)
            ON CONFLICT (connector_id,contract_version) DO UPDATE SET
              requires_retry_review=EXCLUDED.requires_retry_review,
              revision=baseline_review_policies.revision+1""").format(sql.Identifier(self.t.schema)),
              (connector_id,contract_version,required))

    def _provision(self, statement, parameters):
        try:
            with psycopg.connect(**local_admin_settings()) as admin:
                for option,value in NO_SECRET_LOGGING.items():
                    admin.execute(sql.SQL("SET LOCAL {} = {}").format(sql.Identifier(option),sql.Literal(value)))
                admin.execute(statement,parameters)
        except Exception as error:
            raise RuntimeError(f"Baseline provisioning failed: {type(error).__name__}") from None

    def stop(self):
        if self.engine:
            self.engine.dispose()
        if not self.created:
            return
        with psycopg.connect(**local_admin_settings(),autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s",(self.role,))
            with admin.transaction():
                # The role/schema identities were generated by this object and
                # only become owned after their creation transaction committed.
                admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.t.schema)))
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(self.role)))
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(self.owner)))
        self.created = False
