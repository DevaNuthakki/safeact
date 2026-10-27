"""Disposable-only, one-time fake-provider execution gate.

Never deploy as a production execution path. No real provider,
network call, schema migration, retry, or reclaim exists here.

The gate only operates inside a disposable ``safeact_it_<hex32>``
schema; table identifiers are fixed and built from that validated
schema name, never from caller-supplied SQL.

Known test-only limits:
- ``after_commit_for_test`` is a crash-injection hook.
- WORKER_AUDIENCE intentionally differs from
  ``core.worker_authority.WORKER_AUDIENCE``; this gate is not the
  reviewed worker-authority boundary.
- ``worker_subject`` is recorded but not bound to the database
  session (JWT-to-session binding is an open gate).
"""

import re

from threading import Lock
from types import MappingProxyType
from uuid import UUID, uuid4

import sqlalchemy as sa

from core.verified_identity import IdentityVerifier
from storage.submission import (
    IntentObservationStatus,
    observe_intent,
)


WORKER_ISSUER = "https://workers.safeact.example"
WORKER_AUDIENCE = "safeact-fake-execution"

DISPOSABLE_SCHEMA = re.compile(r"safeact_it_[0-9a-f]{32}")

CLAIM_TABLE = "safeact_experimental_claims"
FENCE_TABLE = "safeact_experimental_risk_fences"
INVOCATION_TABLE = "safeact_experimental_invocations"


class WorkerExecutionDenied(Exception):
    pass


def disposable_table(schema, table):
    if type(schema) is not str or not DISPOSABLE_SCHEMA.fullmatch(
        schema
    ):
        raise ValueError("Disposable test schema required")

    if table not in (
        "safeact_operations",
        "safeact_attempts",
        CLAIM_TABLE,
        FENCE_TABLE,
        INVOCATION_TABLE,
    ):
        raise ValueError("Unknown experimental table")

    return f'"{schema}"."{table}"'


class IndependentFakeProvider:
    """No network access or real provider implementation."""

    def __init__(self):
        self._lock = Lock()
        self._calls = []
        self._effects = []
        self.fail_after_effect = False

    def invoke(self, operation_id):
        with self._lock:
            self._calls.append(operation_id)

            # A fake irreversible effect, independently
            # observed from the gate's returned decision.
            self._effects.append(
                (operation_id, uuid4())
            )

        if self.fail_after_effect:
            raise ConnectionError(
                "synthetic provider response lost after effect"
            )

    def snapshot(self):
        with self._lock:
            return (
                tuple(self._calls),
                tuple(self._effects),
            )


class ExperimentalFakeExecutionGate:
    """Disposable-only one-time fake-provider invocation gate."""

    def __init__(
        self,
        *,
        engine,
        identity_verifier,
        worker_authority,
        submission,
        schema,
        provider,
        commit_engine=None,
        after_commit_for_test=None,
    ):
        if type(identity_verifier) is not IdentityVerifier:
            raise TypeError(
                "Real configured identity verifier required"
            )

        if type(worker_authority) is not dict:
            raise TypeError(
                "Trusted worker authority snapshot required"
            )

        operations = disposable_table(schema, "safeact_operations")
        attempts = disposable_table(schema, "safeact_attempts")
        claims = disposable_table(schema, CLAIM_TABLE)
        fences = disposable_table(schema, FENCE_TABLE)
        invocations = disposable_table(schema, INVOCATION_TABLE)

        self._engine = engine
        self._commit_engine = (
            engine if commit_engine is None else commit_engine
        )
        self._verifier = identity_verifier
        self._authority = MappingProxyType(
            dict(worker_authority)
        )
        self._submission = submission
        self._provider = provider
        self._after_commit = after_commit_for_test

        self._consume_sql = sa.text(
            f'INSERT INTO {invocations} ('
            f'invocation_id, operation_id, claim_id, '
            f'attempt_id, fence_id, worker_subject, '
            f'event_type, dispatch_enabled'
            f') '
            f'SELECT '
            f':invocation_id, '
            f'o.operation_id, '
            f'c.claim_id, '
            f'a.attempt_id, '
            f'f.fence_id, '
            f':worker_subject, '
            f"'FAKE_INVOCATION_CONSUMED', FALSE "
            f'FROM {operations} o '
            f'JOIN {claims} c '
            f'ON c.operation_id = o.operation_id '
            f'JOIN {attempts} a '
            f'ON a.operation_id = o.operation_id '
            f'JOIN {fences} f '
            f'ON f.operation_id = o.operation_id '
            f'AND f.claim_id = c.claim_id '
            f'AND f.attempt_id = a.attempt_id '
            f'WHERE o.operation_id = :operation_id '
            f'AND o.tenant_id = :tenant_id '
            f'AND c.claim_id = :claim_id '
            f'AND c.worker_label = :worker_label '
            f'AND c.dispatch_enabled = FALSE '
            f'AND a.attempt_id = :attempt_id '
            f"AND a.dispatch_risk = 'PREPARED' "
            f'AND f.fence_id = :fence_id '
            f"AND f.event_type = 'DISPATCH_RISK_COMMITTED' "
            f'AND f.dispatch_enabled = FALSE '
            f'ON CONFLICT DO NOTHING '
            f'RETURNING invocation_id'
        )

    def run_once(
        self,
        *,
        token,
        operation_id,
        claim_id,
        attempt_id,
        fence_id,
    ):
        # Token validation occurs before any DB observation.
        principal = self._verifier.verify(token)

        authorized = self._authority.get(
            (principal.issuer, principal.subject)
        )

        if (
            authorized is None
            or type(operation_id) is not UUID
            or type(claim_id) is not UUID
            or type(attempt_id) is not UUID
            or type(fence_id) is not UUID
            or authorized[0] != operation_id
        ):
            raise WorkerExecutionDenied(
                "Untrusted worker or operation"
            )

        _, tenant_id, worker_label = authorized

        # A trusted, fixed submission constructed by the
        # caller's fixture, not request-supplied data.
        observation = observe_intent(
            self._engine,
            self._submission,
        )

        if (
            observation.status
            is not IntentObservationStatus.CONFIRMED_RECORDED
            or observation.operation_id != operation_id
        ):
            return False

        # Only a newly inserted and successfully COMMITTED
        # consumption record may approach the fake provider.
        with self._commit_engine.begin() as db:
            consumed = db.execute(
                self._consume_sql,
                {
                    "invocation_id": uuid4(),
                    "operation_id": operation_id,
                    "claim_id": claim_id,
                    "attempt_id": attempt_id,
                    "fence_id": fence_id,
                    "worker_subject": principal.subject,
                    "tenant_id": tenant_id,
                    "worker_label": worker_label,
                },
            ).scalar_one_or_none()

        # A failed or uncertain commit never reaches here.
        if consumed is None:
            return False

        # Test-only crash injection, AFTER durable COMMIT
        # but BEFORE any fake-provider call.
        if self._after_commit is not None:
            self._after_commit()

        self._provider.invoke(operation_id)
        return True
