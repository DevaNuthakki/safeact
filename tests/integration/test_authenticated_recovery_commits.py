"""Real PostgreSQL admission recovery inspection regressions.

Uses real RS256 verification, authority resolution, exact-scope
authorization, SQLAlchemy PostgreSQL transactions and independent
database observation.

Every test owns a randomly named isolated schema in the SafeAct
local development database. No provider dispatch is implemented.

Synthetic post-commit acknowledgment loss is NOT a real transport
interruption during PostgreSQL COMMIT.

Tests are explicitly opt-in and do not establish production dispatch
enforcement or live authority revocation.
"""

import json
import os
import re
import time
import unittest

from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from unittest.mock import patch
from uuid import uuid4

import jwt
import sqlalchemy as sa

from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.pool import NullPool

from core.admission_authorization import (
    AdmissionDenied,
    AdmissionGrant,
    RequestedIntent,
)
from core.authority_registry import (
    ActorAuthorityRecord,
    AuthorityRegistry,
)
from core.contracts import canonical_payload_bytes
from core.verified_identity import (
    IdentityVerificationError,
    IdentityVerifier,
)
from storage.authenticated_submission import (
    AuthenticatedAdmissionService,
)
from storage.database import local_database_url
from storage.recovery_inspection import (
    RecoveryInspectionVerdict,
    inspect_recovery_admission,
)
from storage.schema import (
    metadata,
    operations,
    attempts,
    audit_events,
    outbox_events,
)
from storage.submission import (
    IntentSubmission,
    SubmissionConflict,
    SubmissionStateUncertain,
    record_intent,
)

ISSUER = "https://id.safeact.example"
AUDIENCE = "safeact-admission"

OBSERVATION_TARGET = (
    "storage.authorized_submission.observe_intent"
)


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit opt-in required for isolated PostgreSQL tests",
)
class AuthenticatedRecoveryCommitTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.signing_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        cls.attacker_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        cls.public_jwk = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(
                cls.signing_key.public_key()
            )
        )
        cls.public_jwk.update(
            kid="safeact-recovery-pg-test",
            alg="RS256",
            use="sig",
        )

    def setUp(self):
        self.schema_name = "safeact_it_" + uuid4().hex

        if not re.fullmatch(
            r"safeact_it_[0-9a-f]{32}",
            self.schema_name,
        ):
            raise RuntimeError("STOP: Invalid isolated schema name")

        url = local_database_url()

        if (
            url.drivername != "postgresql+psycopg"
            or url.username != "safeact_dev"
            or url.host != "127.0.0.1"
            or url.database != "safeact_dev"
            or url.port is None
        ):
            raise RuntimeError(
                "STOP: Refusing unexpected database destination"
            )

        self.runtime_engine = None

        self.admin_engine = sa.create_engine(
            url,
            poolclass=NullPool,
            connect_args={"connect_timeout": 5},
        )

        # Register cleanup before any schema-creation attempt.
        self.addCleanup(self.cleanup_schema)

        with self.admin_engine.begin() as db:
            self.assert_database_identity(db)

            db.execute(
                sa.text(
                    f'CREATE SCHEMA "{self.schema_name}"'
                )
            )

        self.runtime_engine = sa.create_engine(
            url,
            poolclass=NullPool,
            connect_args={
                "connect_timeout": 5,
                "options": (
                    f"-c search_path={self.schema_name},public"
                ),
            },
        )

        with self.runtime_engine.connect() as db:
            current = db.execute(
                sa.text("SELECT current_schema()")
            ).scalar_one()

            if current != self.schema_name:
                raise RuntimeError(
                    "STOP: Isolated search_path not active"
                )

            existing = sa.inspect(db).get_table_names(
                schema=self.schema_name
            )

            if existing:
                raise RuntimeError(
                    "STOP: Isolated schema was not empty"
                )

        metadata.create_all(
            self.runtime_engine,
            checkfirst=False,
        )

        with self.runtime_engine.begin() as db:
            created = set(
                sa.inspect(db).get_table_names(
                    schema=self.schema_name
                )
            )

            if created != set(metadata.tables):
                raise RuntimeError(
                    "STOP: Expected schema tables not created"
                )

            for table in metadata.tables:
                if not re.fullmatch(
                    r"[a-z][a-z0-9_]*",
                    table,
                ):
                    raise RuntimeError(
                        "STOP: Unsafe table identifier"
                    )

                db.execute(
                    sa.text(
                        f'CREATE TRIGGER "{table}_write_guard" '
                        f'BEFORE UPDATE OR DELETE '
                        f'ON "{self.schema_name}"."{table}" '
                        f'FOR EACH ROW EXECUTE FUNCTION '
                        f'public.safeact_reject_mutation()'
                    )
                )

        self.verifier = IdentityVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            public_jwks=(self.public_jwk,),
        )

        self.grant = AdmissionGrant(
            tenant_id="tenant-alpha",
            actor_id="actor-alpha",
            connector_id="mock-crm",
            provider_account_id="account-alpha",
            action_type="create_case",
            resource_id="case-123",
            contract_version="v1",
        )

        self.record = ActorAuthorityRecord(
            issuer=ISSUER,
            subject="subject-alpha",
            tenant_id="tenant-alpha",
            actor_id="actor-alpha",
            grants=(self.grant,),
        )

        self.request = RequestedIntent(
            request_key="request-123",
            action_type="create_case",
            resource_id="case-123",
            provider_account_id="account-alpha",
            connector_id="mock-crm",
            contract_version="v1",
            payload={"case_id": "case-123"},
        )

        self.service = self.make_service()

    @staticmethod
    def assert_database_identity(connection):
        database, role = connection.execute(
            sa.text(
                "SELECT current_database(), current_user"
            )
        ).one()

        if (
            database != "safeact_dev"
            or role != "safeact_dev"
        ):
            raise RuntimeError(
                "STOP: Unexpected database or role"
            )

    def cleanup_schema(self):
        if not re.fullmatch(
            r"safeact_it_[0-9a-f]{32}",
            self.schema_name,
        ):
            raise RuntimeError(
                "STOP: Unsafe cleanup schema"
            )

        try:
            if self.runtime_engine is not None:
                self.runtime_engine.dispose()

            with self.admin_engine.begin() as db:
                self.assert_database_identity(db)

                db.execute(
                    sa.text(
                        f'DROP SCHEMA IF EXISTS '
                        f'"{self.schema_name}" CASCADE'
                    )
                )
        finally:
            self.admin_engine.dispose()

    def make_service(self, *, engine=None, records=None):
        registry = AuthorityRegistry(
            audience=AUDIENCE,
            records=(
                (self.record,)
                if records is None else records
            ),
        )

        return AuthenticatedAdmissionService(
            engine=(
                self.runtime_engine
                if engine is None else engine
            ),
            identity_verifier=self.verifier,
            authority_registry=registry,
        )

    def sign(self, changes=None, *, key=None):
        now = int(time.time())

        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "subject-alpha",
            "iat": now - 20,
            "nbf": now - 20,
            "exp": now + 600,
        }

        if changes:
            claims.update(changes)

        return jwt.encode(
            claims,
            self.signing_key if key is None else key,
            algorithm="RS256",
            headers={
                "kid": "safeact-recovery-pg-test"
            },
        )

    def inspect(self, *, service=None, token=None, request=None):
        return inspect_recovery_admission(
            admission_service=(
                self.service if service is None else service
            ),
            token=(
                self.sign() if token is None else token
            ),
            request=(
                self.request if request is None else request
            ),
        )

    def submission(self):
        return IntentSubmission(
            tenant_id="tenant-alpha",
            actor_id="actor-alpha",
            request_key=self.request.request_key,
            action_type=self.request.action_type,
            resource_id=self.request.resource_id,
            provider_account_id=(
                self.request.provider_account_id
            ),
            connector_id=self.request.connector_id,
            contract_version=self.request.contract_version,
            payload=dict(self.request.payload),
            provider_idempotency_key=(
                self.request.provider_idempotency_key
            ),
        )

    def counts(self):
        # Exact corpus oracle order:
        # operations, attempts, audit, outbox.
        with self.runtime_engine.connect() as db:
            return tuple(
                db.execute(
                    sa.select(
                        sa.func.count()
                    ).select_from(table)
                ).scalar_one()
                for table in (
                    operations,
                    attempts,
                    audit_events,
                    outbox_events,
                )
            )

    def assert_hold(self, result, verdict):
        self.assertEqual(result.verdict, verdict)
        self.assertIs(result.retry_allowed, False)
        self.assertIs(
            result.provider_dispatch_authorized,
            False,
        )

    def test_synthetic_lost_ack_after_real_commit_holds(self):
        engine = self.runtime_engine

        class LoseAckAfterCommit:
            @contextmanager
            def begin(self):
                with engine.begin() as connection:
                    yield connection

                # COMMIT succeeded; error is injected afterward.
                # This is not a real lost network acknowledgment.
                raise ConnectionError(
                    "synthetic post-commit acknowledgment loss"
                )

        service = self.make_service(
            engine=LoseAckAfterCommit()
        )

        with self.assertRaises(ConnectionError):
            service.submit(
                token=self.sign(),
                request=self.request,
            )

        self.assertEqual(
            self.counts(),
            (1, 0, 1, 1),
        )

        result = self.inspect()

        self.assert_hold(
            result,
            RecoveryInspectionVerdict.HOLD_PROVIDER_UNVERIFIED,
        )

        self.assertIsNotNone(result.operation_id)

        # Observation is read-only and creates no replay.
        self.assertEqual(
            self.counts(),
            (1, 0, 1, 1),
        )

    def test_inflight_unknown_then_recorded_after_commit(self):
        with self.runtime_engine.connect() as writer:
            transaction = writer.begin()

            try:
                receipt = record_intent(
                    writer,
                    self.submission(),
                )

                before = self.inspect()

                self.assert_hold(
                    before,
                    RecoveryInspectionVerdict.HOLD_ADMISSION_UNKNOWN,
                )

                self.assertIsNone(before.operation_id)

                # A separate connection cannot observe
                # the uncommitted initial bundle.
                self.assertEqual(
                    self.counts(),
                    (0, 0, 0, 0),
                )

                transaction.commit()

            finally:
                if transaction.is_active:
                    transaction.rollback()

        after = self.inspect()

        self.assert_hold(
            after,
            RecoveryInspectionVerdict.HOLD_PROVIDER_UNVERIFIED,
        )

        self.assertEqual(
            after.operation_id,
            receipt.operation_id,
        )

        self.assertEqual(
            self.counts(),
            (1, 0, 1, 1),
        )

    def test_incomplete_committed_bundle_remains_held(self):
        wire = canonical_payload_bytes(
            self.request.payload
        ).decode("utf-8")

        with self.runtime_engine.begin() as db:
            db.execute(
                sa.insert(operations).values(
                    operation_id=uuid4(),
                    tenant_id="tenant-alpha",
                    request_key=self.request.request_key,
                    action_type=self.request.action_type,
                    resource_id=self.request.resource_id,
                    provider_account_id=(
                        self.request.provider_account_id
                    ),
                    connector_id=self.request.connector_id,
                    contract_version=self.request.contract_version,
                    payload_sha256=sha256(
                        wire.encode("utf-8")
                    ).hexdigest(),
                    payload_canonical=wire,
                    provider_idempotency_key=(
                        self.request.provider_idempotency_key
                    ),
                )
            )

        result = self.inspect()

        self.assert_hold(
            result,
            RecoveryInspectionVerdict.HOLD_ADMISSION_UNKNOWN,
        )

        self.assertIsNone(result.operation_id)

        with self.assertRaises(SubmissionStateUncertain):
            self.service.submit(
                token=self.sign(),
                request=self.request,
            )

        self.assertEqual(
            self.counts(),
            (1, 0, 0, 0),
        )

    def test_missing_admission_holds_without_write(self):
        self.assertEqual(
            self.counts(),
            (0, 0, 0, 0),
        )

        result = self.inspect()

        self.assert_hold(
            result,
            RecoveryInspectionVerdict.HOLD_ADMISSION_UNKNOWN,
        )

        self.assertEqual(
            self.counts(),
            (0, 0, 0, 0),
        )

    def test_different_authenticated_actor_cannot_observe(self):
        self.service.submit(
            token=self.sign(),
            request=self.request,
        )

        other_grant = replace(
            self.grant,
            actor_id="actor-beta",
        )

        other_record = ActorAuthorityRecord(
            issuer=ISSUER,
            subject="subject-beta",
            tenant_id="tenant-alpha",
            actor_id="actor-beta",
            grants=(other_grant,),
        )

        other_service = self.make_service(
            records=(
                self.record,
                other_record,
            )
        )

        with self.assertRaises(SubmissionConflict):
            self.inspect(
                service=other_service,
                token=self.sign({
                    "sub": "subject-beta"
                }),
            )

        self.assertEqual(
            self.counts(),
            (1, 0, 1, 1),
        )

    def test_wrong_resource_denied_before_observation(self):
        request = replace(
            self.request,
            resource_id="unauthorized-resource",
        )

        with patch(OBSERVATION_TARGET) as observer:
            with self.assertRaises(AdmissionDenied):
                self.inspect(request=request)

            observer.assert_not_called()

        self.assertEqual(
            self.counts(),
            (0, 0, 0, 0),
        )

    def test_bad_signature_denied_before_observation(self):
        with patch(OBSERVATION_TARGET) as observer:
            with self.assertRaises(
                IdentityVerificationError
            ):
                self.inspect(
                    token=self.sign(
                        key=self.attacker_key
                    )
                )

            observer.assert_not_called()

        self.assertEqual(
            self.counts(),
            (0, 0, 0, 0),
        )

    def test_real_database_read_failure_fails_closed(self):
        def deny_select(
            connection,
            clauseelement,
            multiparams,
            params,
            execution_options,
        ):
            if isinstance(
                clauseelement,
                sa.sql.Select,
            ):
                raise sa.exc.OperationalError(
                    "synthetic database read failure",
                    {},
                    Exception("injected read failure"),
                )

        sa.event.listen(
            self.runtime_engine,
            "before_execute",
            deny_select,
        )

        try:
            with self.assertRaises(
                SubmissionStateUncertain
            ):
                self.inspect()

        finally:
            sa.event.remove(
                self.runtime_engine,
                "before_execute",
                deny_select,
            )

        self.assertEqual(
            self.counts(),
            (0, 0, 0, 0),
        )

    def test_precommit_rollback_remains_unknown(self):
        with self.assertRaises(RuntimeError):
            with self.runtime_engine.begin() as db:
                record_intent(
                    db,
                    self.submission(),
                )

                raise RuntimeError(
                    "synthetic failure before commit"
                )

        result = self.inspect()

        self.assert_hold(
            result,
            RecoveryInspectionVerdict.HOLD_ADMISSION_UNKNOWN,
        )

        self.assertEqual(
            self.counts(),
            (0, 0, 0, 0),
        )


if __name__ == "__main__":
    unittest.main()
