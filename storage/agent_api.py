"""Typed calls into the agent (MCP-role) SQL functions.

Every call runs in the caller's open transaction on a psycopg
connection authenticated as the MCP login role, which may execute only
the safeact_mcp_* functions. The agent key is passed as a bound
parameter and only ever compared as a hash inside PostgreSQL; identity,
tenant and scope are derived there on every call. Refusals raise the
same exceptions as the product API (storage.product_api).
"""

from dataclasses import dataclass
from uuid import UUID

from storage.product_api import _call


@dataclass(frozen=True, slots=True)
class AgentAdmission:
    operation_id: UUID
    created: bool
    request_key: str


def submit(connection, schema, *, key, intent_ref, action_type, resource_id,
           provider_account_id, connector_id, contract_version, payload):
    """Admit (or find) the operation for this intent; never takes a key."""

    return AgentAdmission(*_call(connection, schema, "safeact_mcp_submit", (
        key, intent_ref, action_type, resource_id, provider_account_id,
        connector_id, contract_version, payload,
    )))


def operation(connection, schema, *, key, operation_id):
    return _call(connection, schema, "safeact_mcp_operation", (
        key, operation_id,
    ))[0]


def request_verification(connection, schema, *, key, operation_id):
    return _call(connection, schema, "safeact_mcp_request_verification", (
        key, operation_id,
    ))[0]


def propose(connection, schema, *, key, operation_id, action, rationale):
    return _call(connection, schema, "safeact_mcp_propose", (
        key, operation_id, action, rationale,
    ))[0]
