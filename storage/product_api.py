"""Typed calls into the product (API-role) SQL functions.

Every call runs in the caller's open transaction on a psycopg
connection authenticated as the API login role. The session secret and
CSRF token are passed as bound parameters and only ever compared as
hashes inside PostgreSQL; nothing here interprets them.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg

from storage import durable_lifecycle as lifecycle
from storage import durable_lifecycle_sql as lsql


class ProductRefused(Exception):
    """A product SQL function refused the caller."""


class Unauthenticated(ProductRefused):
    pass


class CsrfRefused(ProductRefused):
    pass


class RoleRequired(ProductRefused):
    pass


class IdentityInvalid(ProductRefused):
    pass


class ActionNotGranted(ProductRefused):
    pass


class RequestConflict(ProductRefused):
    pass


class RequestInvalid(ProductRefused):
    pass


class AlreadyDecided(ProductRefused):
    pass


class SelfReview(ProductRefused):
    pass


class NotEligible(ProductRefused):
    pass


class ConflictingOperation(ProductRefused):
    """An agent's earlier operation on the same target is not settled."""


_ERRORS = {
    lsql.UNAUTHENTICATED: Unauthenticated,
    lsql.CSRF_REFUSED: CsrfRefused,
    lsql.ROLE_REQUIRED: RoleRequired,
    lsql.IDENTITY_INVALID: IdentityInvalid,
    lsql.ACTION_NOT_GRANTED: ActionNotGranted,
    lsql.REQUEST_CONFLICT: RequestConflict,
    lsql.REQUEST_INVALID: RequestInvalid,
    lsql.ALREADY_DECIDED: AlreadyDecided,
    lsql.SELF_REVIEW: SelfReview,
    lsql.NOT_ELIGIBLE: NotEligible,
    lsql.CONFLICTING_OPERATION: ConflictingOperation,
}


@dataclass(frozen=True, slots=True)
class LoginResult:
    ok: bool
    session_secret: str | None
    csrf_token: str | None
    expires_at: datetime | None

    def __repr__(self):
        # Never print the secrets, even in a debugger or a traceback.
        return f"LoginResult(ok={self.ok}, expires_at={self.expires_at!r})"


@dataclass(frozen=True, slots=True)
class Principal:
    principal_id: UUID
    tenant_id: str
    username: str
    display_name: str
    kind: str
    roles: tuple
    demo: bool
    expires_at: datetime


def _call(connection, schema, function, args, *, many=False):
    try:
        return lifecycle._call(connection, schema, function, args, many=many)
    except psycopg.Error as error:
        refused = _ERRORS.get(error.sqlstate)
        if refused is not None:
            # The SQL message is fixed text; it never carries a secret.
            raise refused(str(error).splitlines()[0]) from None
        raise


def login(connection, schema, *, username, password, source):
    """Never raises for bad credentials: the caller must commit."""

    row = _call(
        connection, schema, "safeact_api_login", (username, password, source)
    )
    return LoginResult(*row)


def logout(connection, schema, *, secret, csrf):
    return _call(
        connection, schema, "safeact_api_logout", (secret, csrf)
    )[0]


def whoami(connection, schema, *, secret):
    principal_id, tenant_id, username, display, kind, roles, demo, expires = (
        _call(connection, schema, "safeact_api_whoami", (secret,))
    )
    return Principal(
        principal_id, tenant_id, username, display, kind,
        tuple(roles), demo, expires,
    )


@dataclass(frozen=True, slots=True)
class Admission:
    operation_id: UUID
    created: bool


@dataclass(frozen=True, slots=True)
class WorkflowAdmission:
    workflow_id: UUID
    charge_operation_id: UUID
    created: bool


@dataclass(frozen=True, slots=True)
class OperationSummary:
    operation_id: UUID
    request_key: str
    action_type: str
    resource_id: str
    connector_id: str
    contract_version: str
    state: str
    seq: int
    unknown_reason: str | None
    requested_by: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Grant:
    connector_id: str
    provider_account_id: str
    action_type: str
    resource_id: str
    contract_version: str


@dataclass(frozen=True, slots=True)
class CancelResult:
    outcome: str
    state: str
    seq: int


def submit(connection, schema, *, secret, csrf, request_key, action_type,
           resource_id, provider_account_id, connector_id,
           contract_version, payload_canonical):
    return Admission(*_call(connection, schema, "safeact_api_submit", (
        secret, csrf, request_key, action_type, resource_id,
        provider_account_id, connector_id, contract_version,
        payload_canonical,
    )))


def create_workflow(connection, schema, *, secret, csrf, request_key,
                    customer_id, case_id, sku, fee_minor,
                    payments_account_id, crm_account_id, charge_contract,
                    order_contract, compensation_contract,
                    charge_payload, order_payload):
    return WorkflowAdmission(*_call(
        connection, schema, "safeact_api_create_workflow", (
            secret, csrf, request_key, customer_id, case_id, sku,
            fee_minor, payments_account_id, crm_account_id,
            charge_contract, order_contract, compensation_contract,
            charge_payload, order_payload,
        ),
    ))


def operations(connection, schema, *, secret, state=None, connector=None,
               unresolved_only=False, before=None, before_id=None,
               limit=50):
    rows = _call(connection, schema, "safeact_api_operations", (
        secret, state, connector, unresolved_only, before, before_id, limit,
    ), many=True)
    return [OperationSummary(*row) for row in rows]


def operation(connection, schema, *, secret, operation_id):
    return _call(
        connection, schema, "safeact_api_operation", (secret, operation_id)
    )[0]


def workflow(connection, schema, *, secret, workflow_id):
    return _call(
        connection, schema, "safeact_api_workflow", (secret, workflow_id)
    )[0]


def cancel(connection, schema, *, secret, csrf, operation_id):
    return CancelResult(*_call(
        connection, schema, "safeact_api_cancel", (secret, csrf, operation_id)
    ))


def my_grants(connection, schema, *, secret):
    rows = _call(
        connection, schema, "safeact_api_my_grants", (secret,), many=True
    )
    return [Grant(*row) for row in rows]


def propose(connection, schema, *, secret, csrf, operation_id, action,
            rationale, attested_reference=None):
    return _call(connection, schema, "safeact_api_propose", (
        secret, csrf, operation_id, action, rationale, attested_reference,
    ))[0]


def proposals(connection, schema, *, secret, status="open", limit=50):
    rows = _call(
        connection, schema, "safeact_api_proposals",
        (secret, status, limit), many=True,
    )
    return [row[0] for row in rows]


def proposal(connection, schema, *, secret, proposal_id):
    return _call(
        connection, schema, "safeact_api_proposal", (secret, proposal_id)
    )[0]


def decide(connection, schema, *, secret, csrf, proposal_id, decision,
           binding_digest, decision_key, note=None):
    """A stale binding is a result (outcome refused_stale), not an error,
    so the caller's commit keeps its audit row."""

    return _call(connection, schema, "safeact_api_decide", (
        secret, csrf, proposal_id, decision, binding_digest, decision_key,
        note,
    ))[0]


def changes(connection, schema, *, secret, after):
    """Operations changed since ``after`` (None: just the horizon)."""

    return _call(
        connection, schema, "safeact_api_changes", (secret, after)
    )[0]


def request_verification(connection, schema, *, secret, csrf, operation_id):
    """Queue one lookup-only check inside the contract's bounds."""

    return _call(connection, schema, "safeact_api_request_verification", (
        secret, csrf, operation_id,
    ))[0]
