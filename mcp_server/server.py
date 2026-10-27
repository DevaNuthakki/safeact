"""The guarded SafeAct MCP server (Streamable HTTP, 127.0.0.1 only).

    .venv/bin/python -m mcp_server.server            # demo: port 8002

Each HTTP request must carry ``Authorization: Bearer sak_...``; a
missing or malformed header is refused before MCP sees the request.
The key is not checked here: every tool call passes it to a
safeact_mcp_* function, which derives principal, tenant and scope from
it inside PostgreSQL on that call, as the restricted MCP role. Nothing
about identity is cached.

The low-level MCP server neither validates arguments nor refuses tool
names it did not list, so this module does both: arguments must match
the closed schema exactly, and any other tool name (approve_resolution
included) is refused without touching the database.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import re
import sys
import time

import anyio
import jsonschema
import mcp_types as types
import psycopg
import uvicorn
from mcp.server.lowlevel import Server
from pydantic import ValidationError
from starlette.responses import JSONResponse

from api.catalog import ACTIONS, canonical
from mcp_server import untrusted
from mcp_server.settings import BIND_HOST, McpSettings, demo_settings
from mcp_server.tools import TOOLS, TOOLSET_VERSION
from storage import agent_api as agent
from storage import durable_lifecycle as lifecycle
from storage import product_api as product


log = logging.getLogger("safeact.mcp")

BEARER = re.compile(r"^Bearer (sak_[0-9a-f]{64})$")
DEFAULT_PORT = 8002

# Fixed codes and messages: a refusal never echoes input or SQL text.
REFUSALS = {
    product.Unauthenticated: ("unauthenticated", "The agent key was not accepted."),
    product.RoleRequired: ("forbidden", "This key's scope does not allow this tool."),
    product.IdentityInvalid: ("identity_invalid", "This identity cannot do that."),
    product.ActionNotGranted: ("not_granted", "This action on this target is not granted to you."),
    product.RequestConflict: ("request_conflict", "This intent_ref was already used for a different request."),
    product.RequestInvalid: ("invalid_request", "The request is not valid."),
    product.NotEligible: ("not_eligible", "That is not a permitted next action now."),
    product.ConflictingOperation: (
        "conflicting_operation",
        "An earlier operation on this target is not settled; follow it instead of submitting again.",
    ),
    product.CsrfRefused: ("internal_error", "The request could not be completed."),
    product.AlreadyDecided: ("not_eligible", "That is not a permitted next action now."),
    product.SelfReview: ("forbidden", "This identity cannot do that."),
    lifecycle.UnknownOperation: ("not_found", "No such operation is visible to you."),
}


class Refused(Exception):
    def __init__(self, code, message):
        super().__init__(code)
        self.code = code
        self.message = message


def fingerprint(key):
    """A short, non-reversible label for logs; never the key."""

    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _refusal(error):
    for kind, (code, message) in REFUSALS.items():
        if isinstance(error, kind):
            return Refused(code, message)
    if isinstance(error, lifecycle.LifecycleRejected):
        return Refused("conflict", "SafeAct refused that in the operation's current state.")
    return None


class Tools:
    """The five tools, each one short transaction as the MCP role."""

    def __init__(self, settings, connect=None):
        self.settings = settings
        self.schema = settings.schema
        self.connect = connect or (
            lambda: psycopg.connect(**settings.connect_kwargs())
        )

    def _run(self, action):
        connection = self.connect()
        try:
            with connection.transaction():
                return action(connection)
        except (product.ProductRefused, lifecycle.LifecycleRejected) as error:
            refused = _refusal(error)
            if refused is None:
                raise
            raise refused from None
        finally:
            connection.close()

    def _detail(self, key, operation_id):
        return self._run(lambda c: agent.operation(
            c, self.schema, key=key, operation_id=operation_id,
        ))

    def submit_action(self, key, args):
        spec = ACTIONS[args["action"]]
        try:
            payload = spec.payload.model_validate(args["payload"])
        except ValidationError:
            raise Refused(
                "invalid_request", "The payload is not valid for this action."
            ) from None

        def act(connection):
            admitted = agent.submit(
                connection, self.schema, key=key,
                intent_ref=args["intent_ref"], action_type=args["action"],
                resource_id=getattr(payload, spec.resource_field),
                provider_account_id=args["provider_account_id"],
                connector_id=spec.connector_id,
                contract_version=args.get("contract_version", "v1"),
                payload=canonical(payload),
            )
            detail = agent.operation(
                connection, self.schema, key=key,
                operation_id=admitted.operation_id,
            )
            return admitted, detail

        admitted, detail = self._run(act)
        return {"created": admitted.created, **untrusted.status(detail)}

    def get_action_status(self, key, args):
        current = untrusted.status(self._detail(key, args["operation_id"]))
        return current

    def inspect_evidence(self, key, args):
        return untrusted.evidence(self._detail(key, args["operation_id"]))

    def request_verification(self, key, args):
        result = self._run(lambda c: agent.request_verification(
            c, self.schema, key=key, operation_id=args["operation_id"],
        ))
        return {
            "request_id": result["request_id"],
            "operation_id": result["operation_id"],
            "status": result["status"],
        }

    def propose_resolution(self, key, args):
        proposal = self._run(lambda c: agent.propose(
            c, self.schema, key=key, operation_id=args["operation_id"],
            action=args["action"], rationale=args["rationale"],
        ))
        return {
            "proposal_id": proposal["proposal_id"],
            "operation_id": proposal["operation_id"],
            "action": proposal["action"],
            "status": proposal.get("status", "open"),
            "expires_at": proposal.get("expires_at"),
            "note": "A human reviewer decides. You cannot approve it.",
        }


def _validators():
    return {
        name: jsonschema.Draft202012Validator(tool["input_schema"])
        for name, tool in TOOLS.items()
    }


def _result(payload, *, error=False):
    return types.CallToolResult(
        content=[types.TextContent(
            type="text", text=json.dumps(payload, sort_keys=True, default=str),
        )],
        structured_content=payload,
        is_error=error,
    )


def build_server(settings, *, connect=None):
    tools = Tools(settings, connect=connect)
    validators = _validators()
    listed = [
        types.Tool(name=name, description=tool["description"],
                   input_schema=tool["input_schema"])
        for name, tool in TOOLS.items()
    ]

    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=listed)

    async def call_tool(ctx, params):
        started = time.monotonic()
        name = params.name
        arguments = params.arguments or {}
        key = _bearer(ctx.request)
        outcome = "ok"
        try:
            if name not in TOOLS:
                raise Refused("unknown_tool", "No such tool.")
            if not validators[name].is_valid(arguments):
                raise Refused(
                    "invalid_arguments",
                    "The arguments do not match this tool's schema.",
                )
            if key is None:
                raise Refused("unauthenticated", "The agent key was not accepted.")
            if name == "get_action_status":
                payload = await _wait_status(tools, settings, key, arguments)
            else:
                payload = await anyio.to_thread.run_sync(
                    getattr(tools, name), key, arguments
                )
            return _result(payload)
        except Refused as refused:
            outcome = refused.code
            return _result(
                {"error": refused.code, "message": refused.message}, error=True
            )
        except Exception:
            outcome = "internal_error"
            log.exception("mcp tool failed", extra={"tool": name[:64]})
            return _result({
                "error": "internal_error",
                "message": "The request could not be completed.",
            }, error=True)
        finally:
            log.info("mcp tool call", extra={
                "tool": name[:64],
                "outcome": outcome,
                "agent_key": fingerprint(key) if key else None,
                "duration_ms": round((time.monotonic() - started) * 1000),
            })

    return Server(
        "safeact", version=TOOLSET_VERSION,
        instructions=(
            "SafeAct executes external actions at most once and tells you "
            "what is known. An unknown outcome is not a failure: never "
            "resubmit to find out. You cannot approve anything."
        ),
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def _outcome_phase(state):
    return "unknown" if state in ("UNKNOWN", "VERIFYING") else state


async def _wait_status(tools, settings, key, arguments):
    wait = min(float(arguments.get("wait_seconds", 0)), settings.max_wait_seconds)
    first = await anyio.to_thread.run_sync(tools.get_action_status, key, arguments)
    deadline = time.monotonic() + wait
    current = first
    while not current["terminal"] and time.monotonic() < deadline:
        await asyncio.sleep(min(settings.wait_poll_seconds,
                                max(0.0, deadline - time.monotonic())))
        current = await anyio.to_thread.run_sync(
            tools.get_action_status, key, arguments
        )
        # Wake on news for the caller: a different outcome. UNKNOWN and
        # VERIFYING are the same outcome (still unknown); SafeAct moves
        # between them on every verification cycle without anything new
        # for the caller.
        if _outcome_phase(current["state"]) != _outcome_phase(first["state"]):
            break
    return current


def _bearer(request):
    if request is None:
        return None
    match = BEARER.fullmatch(request.headers.get("authorization", ""))
    return match.group(1) if match else None


class BearerRequired:
    """Refuse any request without a well-formed agent bearer token.

    Only the format is checked here; PostgreSQL decides whether the key
    is valid, on every tool call.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            value = headers.get(b"authorization", b"").decode("latin-1")
            if not BEARER.fullmatch(value):
                response = JSONResponse(
                    {"error": "unauthenticated",
                     "message": "An agent bearer key is required."},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(settings, *, connect=None):
    server = build_server(settings, connect=connect)
    app = server.streamable_http_app(
        stateless_http=True, json_response=True, host=BIND_HOST,
    )
    app.add_middleware(BearerRequired)
    return app


def serve(settings, *, port=DEFAULT_PORT, host=BIND_HOST):
    if host != BIND_HOST:
        raise ValueError("The MCP server binds to 127.0.0.1 only")
    uvicorn.run(
        create_app(settings), host=BIND_HOST, port=port,
        log_level="warning", access_log=False,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    from api.logging import configure
    from observability import metrics
    configure()
    metrics.serve()
    serve(demo_settings(), port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
