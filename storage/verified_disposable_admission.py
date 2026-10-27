"""Verified-token adapter for disposable SafeAct SQL tests only.

This module does not create a secure network boundary.
It does not provision or isolate database credentials.
It never commits, retries, or dispatches provider actions.
"""

from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus

from core.admission_authorization import (
    InvalidAdmissionRequest,
    RequestedIntent,
)
from core.verified_identity import IdentityVerifier
from storage.atomic_gate_sql import gate_call_sql
from storage.submission import (
    SubmissionReceipt,
    SubmissionStateUncertain,
    _prepare_payload,
)


def _valid_text(value, max_length):
    return (
        type(value) is str
        and 1 <= len(value) <= max_length
        and value == value.strip()
        and "*" not in value
        and not any(
            ord(char) < 32 or ord(char) == 127
            for char in value
        )
    )


def _validate_request(request):
    if type(request) is not RequestedIntent:
        raise InvalidAdmissionRequest(
            "RequestedIntent is required"
        )

    limits = (
        ("request_key", 255),
        ("action_type", 128),
        ("resource_id", 255),
        ("provider_account_id", 255),
        ("connector_id", 128),
        ("contract_version", 64),
    )

    for name, limit in limits:
        if not _valid_text(getattr(request, name), limit):
            raise InvalidAdmissionRequest(
                "Invalid requested intent scope"
            )

    key = request.provider_idempotency_key

    if key is not None and not _valid_text(key, 255):
        raise InvalidAdmissionRequest(
            "Invalid provider idempotency key"
        )


class VerifiedDisposableAdmission:
    """Test-only adapter: signed JWT -> canonical SQL admission.

    The caller owns the database transaction, including its
    commit, rollback, and ambiguous-commit reconciliation.

    Only disposable schemas and corresponding temporary
    runtime-role names are accepted.
    """

    __slots__ = (
        "_verifier",
        "_query",
        "_runtime_role",
    )

    def __init__(
        self,
        *,
        identity_verifier: IdentityVerifier,
        schema: str,
    ):
        if type(identity_verifier) is not IdentityVerifier:
            raise ValueError(
                "Trusted IdentityVerifier is required"
            )

        # The builder fails closed for all non-disposable schemas.
        query = gate_call_sql(schema)

        self._verifier = identity_verifier
        self._query = query
        self._runtime_role = (
            "safeact_runtime_it_" + schema[len("safeact_it_"):]
        )

    def submit(
        self,
        *,
        token: str,
        request: RequestedIntent,
        connection: psycopg.Connection,
    ) -> SubmissionReceipt:
        _validate_request(request)

        # No database calls may occur before this verification.
        principal = self._verifier.verify(token)

        # Always derive canonical bytes/digest internally.
        wire, digest = _prepare_payload(request.payload)

        if not isinstance(connection, psycopg.Connection):
            raise TypeError(
                "An explicit psycopg connection is required"
            )

        if (
            connection.closed
            or connection.info.transaction_status
            != TransactionStatus.INTRANS
        ):
            raise RuntimeError(
                "An active outer database transaction is required"
            )

        # This detects a misconfigured test connection.
        # It does not protect against compromised privileged code.
        effective_role = connection.execute(
            "SELECT current_user"
        ).fetchone()[0]

        if effective_role != self._runtime_role:
            raise PermissionError(
                "Unexpected database execution role"
            )

        row = connection.execute(
            self._query,
            (
                principal.issuer,
                principal.subject,
                request.request_key,
                request.action_type,
                request.resource_id,
                request.connector_id,
                request.provider_account_id,
                request.contract_version,
                wire,
                digest,
                request.provider_idempotency_key,
            ),
        ).fetchone()

        if (
            row is None
            or type(row[0]) is not UUID
            or type(row[1]) is not bool
        ):
            raise SubmissionStateUncertain(
                "Database admission returned invalid evidence"
            )

        return SubmissionReceipt(
            operation_id=row[0],
            created=row[1],
        )
