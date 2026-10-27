"""Disposable PostgreSQL fixture for the durable lifecycle.

Creates a ``safeact_it_<hex32>`` schema with the foundation tables,
their write guards, every lifecycle migration from 0003 to head, the
mock connector contracts, and test-only roles:

- owner: NOLOGIN, owns the protected functions, minimum privileges
- worker_a / worker_b: LOGIN, may claim, dispatch, record outcomes
- recoverer: LOGIN, may run recovery transitions
- advancer: LOGIN, may admit the next step of a fixed workflow
- stranger: LOGIN, no grants at all

Per-role passwords are random and test-only. The real database
``session_user`` identifies each caller; JWT-to-session binding is
still an open gate. Not a test module (no ``test_`` prefix).
"""

import re
import secrets

from dataclasses import dataclass, replace
from uuid import uuid4

import psycopg
import sqlalchemy as sa

from psycopg import sql
from sqlalchemy.pool import NullPool

from core.attempts import AttemptOutcome
from core.connector_profiles import MOCK_PROFILES
from core.contracts import EvidenceKind
from core.lifecycle import LifecycleState as S
from storage import durable_lifecycle as lifecycle
from storage.database import local_database_url
from storage.lifecycle_environment import (  # noqa: F401
    FOUNDATION_TABLES,
    register_statements,
    schema_statements,
)
from storage.state_projection import projection_drift
from storage.submission import IntentSubmission, submit_intent
from tests.integration.profiles_fixture import TEST_PROFILES
from tests.integration.test_postgres_smoke import database_settings



BASE_REQUEST = IntentSubmission(
    tenant_id="synthetic-tenant",
    actor_id="synthetic-actor",
    request_key="lifecycle-request",
    action_type="refund_payment",
    resource_id="synthetic-charge",
    provider_account_id="mock-payments-account",
    connector_id="mock-payments",
    contract_version="v1",
    payload={"charge_id": "synthetic-charge", "amount_minor": 1200},
)


@dataclass
class Driven:
    operation_id: object
    claim: object = None
    fence: object = None
    seq: int = 0


def commit(connection, action):
    """Run one protected call in its own committed transaction."""

    try:
        result = action()
        connection.commit()
        return result
    except BaseException:
        connection.rollback()
        raise


HISTORY_CHAINS = {
    "DISPATCHING": (
        ("RECORDED", "CLAIMED"),
        ("CLAIMED", "DISPATCHING"),
    ),
    "SUCCEEDED": (
        ("RECORDED", "CLAIMED"),
        ("CLAIMED", "DISPATCHING"),
        ("DISPATCHING", "SUCCEEDED"),
    ),
    "UNKNOWN": (
        ("RECORDED", "CLAIMED"),
        ("CLAIMED", "DISPATCHING"),
        ("DISPATCHING", "UNKNOWN"),
    ),
    "VERIFYING": (
        ("RECORDED", "CLAIMED"),
        ("CLAIMED", "DISPATCHING"),
        ("DISPATCHING", "UNKNOWN"),
        ("UNKNOWN", "VERIFYING"),
    ),
}


class DisposableLifecycleDatabase:

    def __init__(self):
        suffix = uuid4().hex
        self.schema = "safeact_it_" + suffix

        if not re.fullmatch(r"safeact_it_[0-9a-f]{32}", self.schema):
            raise RuntimeError("STOP: invalid disposable schema")

        self.owner = "safeact_fn_it_" + suffix
        self.worker_a = "safeact_wk_it_" + suffix + "_a"
        self.worker_b = "safeact_wk_it_" + suffix + "_b"
        self.recoverer = "safeact_rc_it_" + suffix
        self.stranger = "safeact_xx_it_" + suffix
        self.advancer = "safeact_av_it_" + suffix

        self.login_roles = (
            self.worker_a, self.worker_b,
            self.recoverer, self.stranger, self.advancer,
        )
        self.passwords = {
            role: secrets.token_urlsafe(32)
            for role in self.login_roles
        }

        self.settings = database_settings()
        self.admin = None
        self.engine = None
        self._connections = []
        self._created_roles = []
        self._schema_created = False

    # -- lifecycle ---------------------------------------------------

    def start(self):
        if (
            self.settings["host"] != "127.0.0.1"
            or self.settings["dbname"] != "safeact_dev"
        ):
            raise RuntimeError("STOP: local dev database required")

        self.admin = psycopg.connect(**self.settings, autocommit=True)
        S = sql.Identifier(self.schema)

        self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(S))
        self._schema_created = True

        self.admin.execute(sql.SQL(
            "CREATE ROLE {} NOLOGIN NOINHERIT NOSUPERUSER "
            "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        ).format(sql.Identifier(self.owner)))
        self._created_roles.append(self.owner)

        for role in self.login_roles:
            self.admin.execute(sql.SQL(
                "CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER "
                "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS "
                "PASSWORD {}"
            ).format(
                sql.Identifier(role),
                sql.Literal(self.passwords[role]),
            ))
            self._created_roles.append(role)

        self.engine = sa.create_engine(
            local_database_url(),
            poolclass=NullPool,
            connect_args={
                "connect_timeout": 5,
                "options": f"-c search_path={self.schema},public",
            },
        )

        with self.admin.transaction():
            for statement in schema_statements(
                self.schema,
                owner=self.owner,
                workers=(self.worker_a, self.worker_b),
                recoverers=(self.recoverer,),
                advancers=(self.advancer,),
            ):
                self.admin.execute(statement)

        self.register_profiles(MOCK_PROFILES + TEST_PROFILES)
        return self

    def register_profiles(self, profiles):
        """Register connector contracts in this disposable schema only."""

        with self.admin.transaction():
            for profile in profiles:
                for statement, params in register_statements(
                    self.schema, profile
                ):
                    self.admin.execute(statement, params)

    def cleanup(self):
        for connection in self._connections:
            try:
                connection.close()
            except Exception:
                pass

        if self.engine is not None:
            self.engine.dispose()

        if self.admin is None:
            return

        # Every test class leaves the projection equal to the log.
        drift = []
        if self._schema_created:
            try:
                drift = projection_drift(self.admin, self.schema)
            except psycopg.Error as error:
                drift = [f"drift check failed: {type(error).__name__}"]

        try:
            self.admin.execute("SET lock_timeout = '5000ms'")

            if self._created_roles:
                self.admin.execute(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE usename = ANY(%s)",
                    (self._created_roles,),
                )

            if self._schema_created:
                self.admin.execute(sql.SQL(
                    "DROP SCHEMA IF EXISTS {} CASCADE"
                ).format(sql.Identifier(self.schema)))

            for role in reversed(self._created_roles):
                self.admin.execute(sql.SQL(
                    "DROP ROLE IF EXISTS {}"
                ).format(sql.Identifier(role)))
        finally:
            self.admin.close()

        if drift:
            raise AssertionError(
                f"Current-state projection drift: {drift[:3]}"
            )

    # -- helpers -----------------------------------------------------

    def connect(self, role, **kwargs):
        settings = dict(self.settings)
        settings.update(
            user=role,
            password=self.passwords[role],
        )
        settings.update(kwargs)

        connection = psycopg.connect(**settings)
        self._connections.append(connection)
        return connection

    def request(self, **changes):
        # The provider key is derived server-side from the identity.
        changes.setdefault("provider_idempotency_key", None)
        return replace(BASE_REQUEST, **changes)

    def provider_key(self, operation_id):
        return self.admin.execute(
            sql.SQL(
                "SELECT provider_idempotency_key FROM {} "
                "WHERE operation_id = %s"
            ).format(self.table("safeact_operations")),
            (operation_id,),
        ).fetchone()[0]

    def admit(self, **changes):
        receipt = submit_intent(self.engine, self.request(**changes))

        if not receipt.created:
            raise RuntimeError("Fixture expected a new operation")
        return receipt.operation_id

    def table(self, name):
        return sql.SQL("{}.{}").format(
            sql.Identifier(self.schema), sql.Identifier(name)
        )

    def count(self, table, **where):
        query = sql.SQL("SELECT count(*) FROM {}").format(
            self.table(table)
        )
        if where:
            query += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(
                sql.SQL("{} = %s").format(sql.Identifier(column))
                for column in where
            )
        return self.admin.execute(
            query, tuple(where.values())
        ).fetchone()[0]

    def drive_to(self, operation_id, target, *, worker, recoverer):
        """Advance through protected functions only; commit each step."""

        target = S(target)
        result = Driven(operation_id)

        if target is S.RECORDED:
            return result

        if target is S.CANCELLED_BEFORE_CLAIM:
            # The same protected append the API's cancel function makes
            # (this lifecycle-only schema has no API role or sessions).
            commit(self.admin, lambda: self.admin.execute(sql.SQL(
                "SELECT {}.safeact_lifecycle_append(%s, 1, 'RECORDED', "
                "'CANCELLED_BEFORE_CLAIM', gen_random_uuid(), NULL, NULL, "
                "'cancelled_before_claim', '{{}}'::jsonb, "
                "'LIFECYCLE_CANCELLED')"
            ).format(sql.Identifier(self.schema)), (operation_id,)))
            result.seq = 1
            return result

        result.claim = commit(worker, lambda: lifecycle.claim(
            worker, self.schema, operation_id, lease_seconds=300,
        ))
        result.seq = result.claim.seq
        if target is S.CLAIMED:
            return result

        result.fence = commit(worker, lambda: lifecycle.begin_dispatch(
            worker, self.schema, result.claim, lease_seconds=300,
        ))
        result.seq = result.fence.seq
        if target is S.DISPATCHING:
            return result

        first = (
            target if target in (S.SUCCEEDED, S.FAILED_CONFIRMED)
            else S.UNKNOWN
        )
        if first is not S.UNKNOWN:
            self.prove(
                worker, result.fence.attempt_id,
                applied=first is S.SUCCEEDED,
            )
        result.seq = commit(
            worker, lambda: lifecycle.record_dispatch_outcome(
                worker, self.schema, result.fence, first,
                reason=(
                    "ambiguous_no_response" if first is S.UNKNOWN
                    else "fixture_outcome"
                ),
            )
        )
        if target is first:
            return result

        result.seq = self.recover(
            recoverer, operation_id, result.seq, S.UNKNOWN, S.VERIFYING
        )
        if target is S.VERIFYING:
            return result

        result.seq = self.recover(
            recoverer, operation_id, result.seq, S.VERIFYING, target
        )
        return result

    def prove(self, connection, attempt_id, *, applied, reference=None):
        """Record decisive provider evidence for one attempt."""

        outcome, kind = (
            (AttemptOutcome.CONFIRMED_APPLIED, EvidenceKind.APPLIED)
            if applied else
            (AttemptOutcome.PROVEN_NOT_APPLIED,
             EvidenceKind.PROVEN_NOT_APPLIED)
        )
        return commit(connection, lambda: lifecycle.record_attempt_outcome(
            connection, self.schema, attempt_id, outcome,
            evidence_kind=kind,
            reference=reference or f"fixture-proof-{uuid4().hex}",
        ))

    def recover(self, recoverer, operation_id, seq, expected, target):
        return commit(recoverer, lambda: lifecycle.recovery_transition(
            recoverer, self.schema, operation_id,
            expected_seq=seq, expected_state=expected,
            to_state=target, reason="fixture_recovery",
        ))

    def seed_history(self, groups):
        """Bulk-insert synthetic history as the fixture superuser.

        groups: {prefix: (count, final_state_or_None, age_seconds)}.
        Bypasses the protected functions on purpose; for plan and
        timing measurements only, never for behaviour tests.
        """

        with self.admin.transaction():
            for prefix, (count, final, age) in groups.items():
                self._seed(prefix, count, final, age)
            self.catch_up_projection()

        for table in (
            "safeact_operations",
            "safeact_attempts",
            "safeact_operation_transitions",
            "safeact_state_projection",
        ):
            self.admin.execute(sql.SQL("ANALYZE {}").format(
                self.table(table)
            ))

    def catch_up_projection(self):
        """Advance the projection over transitions inserted directly.

        Fixture-only, as the superuser, for history written around the
        protected functions on purpose. Moves one seq per pass, which is
        the only kind of update the guard trigger accepts.
        """

        step = sql.SQL("""
            UPDATE {p} p
               SET (seq, state, claim_id, attempt_id, actor,
                    lease_expires_at, state_changed_at) = (
                   t.seq, t.to_state, t.claim_id, t.attempt_id, t.actor,
                   t.lease_expires_at, t.created_at
               )
            FROM {transitions} t
            WHERE t.operation_id = p.operation_id AND t.seq = p.seq + 1
        """).format(
            p=self.table("safeact_state_projection"),
            transitions=self.table("safeact_operation_transitions"),
        )
        while self.admin.execute(step).rowcount:
            pass

    def _seed(self, prefix, count, final, age):
        admin, t = self.admin, self.table

        admin.execute(sql.SQL("""
            INSERT INTO {ops} (
                operation_id, tenant_id, request_key, action_type,
                resource_id, provider_account_id, connector_id,
                contract_version, payload_sha256, payload_canonical,
                created_at
            )
            SELECT gen_random_uuid(), 'seed-tenant', {prefix} || g,
                   'refund_payment', 'r', 'p', 'mock-payments', 'v1',
                   encode(sha256(convert_to(
                       '{{"n":' || g || '}}', 'UTF8')), 'hex'),
                   '{{"n":' || g || '}}',
                   now() - make_interval(secs => {age} - g)
            FROM generate_series(1, {count}) g
        """).format(
            ops=t("safeact_operations"),
            prefix=sql.Literal(prefix + "-"),
            age=sql.Literal(age),
            count=sql.Literal(count),
        ))

        if final is None:
            return

        admin.execute(sql.SQL("""
            INSERT INTO {attempts} (attempt_id, operation_id, ordinal)
            SELECT gen_random_uuid(), o.operation_id, 1
            FROM {ops} o WHERE o.request_key LIKE {pattern}
        """).format(
            attempts=t("safeact_attempts"),
            ops=t("safeact_operations"),
            pattern=sql.Literal(prefix + "-%"),
        ))

        steps = sql.SQL(", ").join(
            sql.SQL("({}, {}, {})").format(
                sql.Literal(seq), sql.Literal(a), sql.Literal(b)
            )
            for seq, (a, b) in enumerate(HISTORY_CHAINS[final], start=1)
        )

        admin.execute(sql.SQL("""
            INSERT INTO {transitions} (
                operation_id, seq, from_state, to_state, claim_id,
                attempt_id, actor, lease_expires_at, reason
            )
            SELECT a.operation_id, x.seq, x.f, x.t, gen_random_uuid(),
                   a.attempt_id, 'seed', now(), 'seed'
            FROM {attempts} a
            JOIN {ops} o ON o.operation_id = a.operation_id
            CROSS JOIN (VALUES {steps}) AS x(seq, f, t)
            WHERE o.request_key LIKE {pattern}
        """).format(
            transitions=t("safeact_operation_transitions"),
            attempts=t("safeact_attempts"),
            ops=t("safeact_operations"),
            steps=steps,
            pattern=sql.Literal(prefix + "-%"),
        ))

    def outbox_id(self, operation_id):
        return self.admin.execute(
            sql.SQL(
                "SELECT outbox_id FROM {} WHERE operation_id = %s"
            ).format(self.table("safeact_outbox_events")),
            (operation_id,),
        ).fetchone()[0]
