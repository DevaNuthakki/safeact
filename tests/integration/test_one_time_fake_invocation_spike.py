"""One-time, fake-provider invocation experiment.

No production worker, provider network call, schema
migration, real dispatch, or retry authority exists here.

The experiment reuses a known disposable PostgreSQL
schema fixture through composition, not TestCase inheritance.

The fake provider is instrumented independently from
the gate decision.

Worker authority is an immutable local test snapshot,
not a production worker-identity service.

A consumed gate intentionally cannot be reclaimed.
"""

import json
import os
import time
import unittest

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from uuid import uuid4

import jwt
import sqlalchemy as sa

from cryptography.hazmat.primitives.asymmetric import rsa

from core.verified_identity import (
    IdentityVerificationError,
    IdentityVerifier,
)
from storage.experimental_fake_execution import (
    WORKER_AUDIENCE,
    WORKER_ISSUER,
    ExperimentalFakeExecutionGate,
    IndependentFakeProvider,
    WorkerExecutionDenied,
    disposable_table,
)

# Import the MODULE, not the TestCase class, so unittest
# discovery does not duplicate the existing eight tests.
from tests.integration import (
    test_dispatch_risk_fence_spike as fence_fixture
)


class SyntheticPostcommitAckLoss:
    """Inject a failure AFTER actual PostgreSQL COMMIT."""

    def __init__(self, underlying):
        self._underlying = underlying

    def connect(self):
        return self._underlying.connect()

    @contextmanager
    def begin(self):
        with self._underlying.begin() as db:
            yield db

        raise ConnectionError(
            "synthetic acknowledgment lost after commit"
        )


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Explicit disposable PostgreSQL opt-in required",
)
class OneTimeFakeInvocationSpikeTests(unittest.TestCase):

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
            kid="safeact-fake-worker-key",
            alg="RS256",
            use="sig",
        )

    def setUp(self):
        self.fixture = (
            fence_fixture.DispatchRiskFenceSpikeTests(
                "test_committed_fence_binds_claim_attempt_and_operation"
            )
        )

        # The underlying fixture owns its cleanup registry.
        # Run it even if this test raises unexpectedly.
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

        self.engine = self.fixture.runtime_engine

        schema = self.fixture.schema_name

        self.invocation_table = disposable_table(
            schema,
            "safeact_experimental_invocations",
        )

        s = f'"{schema}"'

        with self.engine.begin() as db:
            db.execute(
                sa.text(
                    f'ALTER TABLE {self.fixture.fence_table} '
                    f'ADD CONSTRAINT '
                    f'"safeact_exp_fence_operation_pair" '
                    f'UNIQUE (operation_id, fence_id)'
                )
            )

            db.execute(
                sa.text(
                    f'CREATE TABLE {self.invocation_table} ('
                    f'invocation_id uuid PRIMARY KEY, '
                    f'operation_id uuid NOT NULL UNIQUE, '
                    f'claim_id uuid NOT NULL, '
                    f'attempt_id uuid NOT NULL, '
                    f'fence_id uuid NOT NULL UNIQUE, '
                    f'worker_subject text NOT NULL '
                    f'CHECK (length(worker_subject) BETWEEN 1 AND 255), '
                    f'event_type text NOT NULL '
                    f'CHECK (event_type = '
                    f"'FAKE_INVOCATION_CONSUMED'), "
                    f'dispatch_enabled boolean NOT NULL '
                    f'DEFAULT false '
                    f'CHECK (dispatch_enabled = false), '
                    f'created_at timestamptz NOT NULL '
                    f'DEFAULT now(), '
                    f'FOREIGN KEY (operation_id, claim_id) '
                    f'REFERENCES {self.fixture.claim_table}'
                    f'(operation_id, claim_id), '
                    f'FOREIGN KEY (operation_id, attempt_id) '
                    f'REFERENCES {s}."safeact_attempts"'
                    f'(operation_id, attempt_id), '
                    f'FOREIGN KEY (operation_id, fence_id) '
                    f'REFERENCES {self.fixture.fence_table}'
                    f'(operation_id, fence_id)'
                    f')'
                )
            )

            db.execute(
                sa.text(
                    f'CREATE TRIGGER '
                    f'"safeact_experimental_invocations_write_guard" '
                    f'BEFORE UPDATE OR DELETE '
                    f'ON {self.invocation_table} '
                    f'FOR EACH ROW EXECUTE FUNCTION '
                    f'public.safeact_reject_mutation()'
                )
            )

        self.verifier = IdentityVerifier(
            issuer=WORKER_ISSUER,
            audience=WORKER_AUDIENCE,
            public_jwks=(self.public_jwk,),
        )

        self.provider = IndependentFakeProvider()
        self.operation_id = None
        self.claim_id = None
        self.attempt_id = None
        self.fence_id = None

    def sign(self, changes=None, *, key=None):
        now = int(time.time())

        claims = {
            "iss": WORKER_ISSUER,
            "aud": WORKER_AUDIENCE,
            "sub": "worker-alpha",
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
            headers={"kid": "safeact-fake-worker-key"},
        )

    def ready(self):
        (
            self.operation_id,
            self.claim_id,
            self.attempt_id,
        ) = self.fixture.ready()

        self.fence_id = self.fixture.commit_fence(
            self.operation_id,
            self.claim_id,
            self.attempt_id,
        )

        self.assertIsNotNone(self.fence_id)

    def gate(self, *, commit_engine=None, after_commit=None):
        return ExperimentalFakeExecutionGate(
            engine=self.engine,
            commit_engine=commit_engine,
            identity_verifier=self.verifier,
            worker_authority={
                (WORKER_ISSUER, "worker-alpha"): (
                    self.operation_id,
                    "synthetic-tenant",
                    "synthetic-worker",
                ),
            },
            submission=self.fixture.request(),
            schema=self.fixture.schema_name,
            provider=self.provider,
            after_commit_for_test=after_commit,
        )

    def run_gate(self, gate=None, *, token=None, **changes):
        values = {
            "token": self.sign() if token is None else token,
            "operation_id": self.operation_id,
            "claim_id": self.claim_id,
            "attempt_id": self.attempt_id,
            "fence_id": self.fence_id,
        }
        values.update(changes)

        return (
            self.gate() if gate is None else gate
        ).run_once(**values)

    def consumed_count(self):
        with self.engine.connect() as db:
            return db.execute(
                sa.text(
                    f'SELECT count(*) FROM {self.invocation_table}'
                )
            ).scalar_one()

    def assert_provider_counts(self, calls, effects):
        observed_calls, observed_effects = (
            self.provider.snapshot()
        )

        self.assertEqual(len(observed_calls), calls)
        self.assertEqual(len(observed_effects), effects)

    def test_first_consumption_invokes_fake_provider_once(self):
        self.ready()

        self.assertTrue(self.run_gate())
        self.assertEqual(self.consumed_count(), 1)
        self.assert_provider_counts(1, 1)

        self.assertEqual(
            self.fixture.bundle_counts(),
            (1, 1, 1, 1),
        )

    def test_eight_contenders_only_one_provider_invocation(self):
        self.ready()
        gate = self.gate()
        token = self.sign()
        barrier = Barrier(8)

        def contender(_):
            barrier.wait(timeout=10)
            return self.run_gate(
                gate=gate,
                token=token,
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(contender, range(8)))

        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 7)
        self.assertEqual(self.consumed_count(), 1)
        self.assert_provider_counts(1, 1)

    def test_replay_does_not_call_provider_again(self):
        self.ready()
        gate = self.gate()

        self.assertTrue(self.run_gate(gate=gate))

        for _ in range(5):
            self.assertFalse(self.run_gate(gate=gate))

        self.assertEqual(self.consumed_count(), 1)
        self.assert_provider_counts(1, 1)

    def test_missing_and_uncommitted_fence_deny_invocation(self):
        self.operation_id, self.claim_id, self.attempt_id = (
            self.fixture.ready()
        )
        self.fence_id = uuid4()
        gate = self.gate()

        self.assertFalse(self.run_gate(gate=gate))
        self.assertEqual(self.consumed_count(), 0)

        with self.engine.connect() as writer:
            transaction = writer.begin()

            try:
                pending = self.fixture.insert_fence(
                    writer,
                    self.operation_id,
                    self.claim_id,
                    self.attempt_id,
                )
                self.assertIsNotNone(pending)
                self.fence_id = pending

                # An independent transaction still cannot
                # observe the uncommitted fence.
                self.assertFalse(self.run_gate(gate=gate))
                self.assertEqual(self.consumed_count(), 0)

                transaction.commit()
            finally:
                if transaction.is_active:
                    transaction.rollback()

        self.assertTrue(self.run_gate(gate=gate))
        self.assertEqual(self.consumed_count(), 1)
        self.assert_provider_counts(1, 1)

    def test_wrong_claim_or_attempt_is_denied(self):
        self.ready()

        other_operation = self.fixture.admit(
            request_key="different-invocation-operation"
        )
        other_attempt = self.fixture.prepare_attempt(
            other_operation
        )
        other_claim = self.fixture.reserve(
            other_operation,
            "synthetic-other-worker",
        )

        self.assertIsNotNone(other_claim)

        gate = self.gate()

        self.assertFalse(
            self.run_gate(
                gate=gate,
                claim_id=other_claim,
            )
        )
        self.assertFalse(
            self.run_gate(
                gate=gate,
                attempt_id=other_attempt,
            )
        )

        self.assertEqual(self.consumed_count(), 0)
        self.assert_provider_counts(0, 0)

    def test_forged_or_wrong_audience_token_is_denied(self):
        self.ready()

        for token in (
            self.sign(key=self.attacker_key),
            self.sign({"aud": "safeact-admission"}),
        ):
            with self.subTest(token_kind=token[:24]):
                with self.assertRaises(
                    IdentityVerificationError
                ):
                    self.run_gate(token=token)

        self.assertEqual(self.consumed_count(), 0)
        self.assert_provider_counts(0, 0)

    def test_unregistered_worker_and_wrong_operation_denied(self):
        self.ready()

        with self.assertRaises(WorkerExecutionDenied):
            self.run_gate(
                token=self.sign({
                    "sub": "worker-unregistered",
                })
            )

        with self.assertRaises(WorkerExecutionDenied):
            self.run_gate(
                operation_id=uuid4()
            )

        self.assertEqual(self.consumed_count(), 0)
        self.assert_provider_counts(0, 0)

    def test_synthetic_postcommit_ack_loss_blocks_call_and_replay(self):
        self.ready()

        unreliable = SyntheticPostcommitAckLoss(
            self.engine
        )

        gate = self.gate(commit_engine=unreliable)

        with self.assertRaises(ConnectionError):
            self.run_gate(gate=gate)

        # Transaction committed, but acknowledgment was
        # synthetically lost BEFORE any provider call.
        self.assertEqual(self.consumed_count(), 1)
        self.assert_provider_counts(0, 0)

        self.assertFalse(self.run_gate())
        self.assert_provider_counts(0, 0)

    def test_crash_or_lost_provider_reply_never_replays(self):
        # First independent operation: crash immediately
        # after gate consumption, before provider call.
        self.ready()

        def crash():
            raise RuntimeError(
                "synthetic worker crash after consumption"
            )

        with self.assertRaises(RuntimeError):
            self.run_gate(
                gate=self.gate(after_commit=crash)
            )

        self.assertEqual(self.consumed_count(), 1)
        self.assert_provider_counts(0, 0)
        self.assertFalse(self.run_gate())

        # Second independent operation uses the same
        # test schema, with a new request key and a new
        # trusted gate authorization binding.
        second_operation = self.fixture.admit(
            request_key="provider-reply-loss-operation"
        )
        second_attempt = self.fixture.prepare_attempt(
            second_operation
        )
        second_claim = self.fixture.reserve(
            second_operation,
            "synthetic-worker",
        )
        self.assertIsNotNone(second_claim)

        second_fence = self.fixture.commit_fence(
            second_operation,
            second_claim,
            second_attempt,
        )
        self.assertIsNotNone(second_fence)

        self.provider.fail_after_effect = True

        second_gate = ExperimentalFakeExecutionGate(
            engine=self.engine,
            identity_verifier=self.verifier,
            worker_authority={
                (WORKER_ISSUER, "worker-alpha"): (
                    second_operation,
                    "synthetic-tenant",
                    "synthetic-worker",
                ),
            },
            submission=self.fixture.request(
                request_key="provider-reply-loss-operation"
            ),
            schema=self.fixture.schema_name,
            provider=self.provider,
        )

        arguments = {
            "token": self.sign(),
            "operation_id": second_operation,
            "claim_id": second_claim,
            "attempt_id": second_attempt,
            "fence_id": second_fence,
        }

        with self.assertRaises(ConnectionError):
            second_gate.run_once(**arguments)

        self.assertEqual(self.consumed_count(), 2)
        self.assert_provider_counts(1, 1)

        self.assertFalse(second_gate.run_once(**arguments))
        self.assert_provider_counts(1, 1)

    def test_gate_rejects_non_disposable_identifiers(self):
        for schema, table in (
            ("public", "safeact_operations"),
            ('safeact_it_' + "0" * 31 + '"', "safeact_operations"),
            (self.fixture.schema_name, "pg_authid"),
            (self.fixture.schema_name, 'x"; DROP TABLE y; --'),
        ):
            with self.subTest(schema=schema, table=table):
                with self.assertRaises(ValueError):
                    disposable_table(schema, table)

        with self.assertRaises(ValueError):
            ExperimentalFakeExecutionGate(
                engine=self.engine,
                identity_verifier=self.verifier,
                worker_authority={},
                submission=self.fixture.request(),
                schema="public",
                provider=self.provider,
            )


if __name__ == "__main__":
    unittest.main()
