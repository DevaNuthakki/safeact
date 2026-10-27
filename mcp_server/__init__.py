"""SafeAct MCP boundary: scoped tools for agents (blueprint 06, Milestone 4).

The guarded server (``mcp_server.server``) exposes five tools to service
principals over Streamable HTTP on 127.0.0.1. Every call carries the
agent's key as a bearer token; identity, tenant and scope are derived
from it inside PostgreSQL on that call (migration 0016). Approval is
never exposed: there is no approve tool, and the MCP database role has
no decision function. Provider-originated text is returned only in
fields marked untrusted.

MCP is an interoperability interface, not the authorization system:
every rule is enforced by PostgreSQL behind the safeact_mcp_* functions.
"""
