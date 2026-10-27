"""The guarded MCP server over real HTTP, for integration tests.

Serves ``mcp_server.server.create_app`` with uvicorn on a free
127.0.0.1 port, as the disposable environment's MCP login role, and
talks to it with the official MCP SDK client (Streamable HTTP), one
fresh client per call. Not a test module (no ``test_`` prefix).
"""

import asyncio
import json
import socket
import threading
import time

import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_server.server import create_app
from mcp_server.settings import McpSettings
from storage.database import local_admin_settings


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class McpServer:

    def __init__(self, api, **overrides):
        admin = local_admin_settings()
        self.settings = McpSettings(
            schema=api.env.schema, role=api.env.mcp,
            password=api.role_passwords[api.env.mcp],
            host=admin["host"], port=admin["port"], dbname=admin["dbname"],
            **overrides,
        )
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.server = None
        self.thread = None

    def start(self):
        config = uvicorn.Config(
            create_app(self.settings), host="127.0.0.1", port=self.port,
            log_level="warning", access_log=False,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("MCP server did not start")
            time.sleep(0.05)
        return self

    def stop(self):
        if self.server is not None:
            self.server.should_exit = True
            self.thread.join(timeout=10)

    # -- client -------------------------------------------------------

    def _headers(self, key):
        return {} if key is None else {"Authorization": f"Bearer {key}"}

    async def _with_client(self, key, action):
        async with httpx2.AsyncClient(headers=self._headers(key)) as http:
            async with Client(
                streamable_http_client(self.url, http_client=http)
            ) as client:
                return await action(client)

    def call(self, key, name, arguments=None):
        """One tool call; returns (is_error, structured payload)."""

        async def action(client):
            return await client.call_tool(name, arguments or {})

        result = asyncio.run(self._with_client(key, action))
        return result.is_error, result.structured_content

    def tools(self, key):
        async def action(client):
            return (await client.list_tools()).tools

        return asyncio.run(self._with_client(key, action))

    def raw_tool_call(self, key, era, name, arguments_text):
        """A tools/call whose arguments are given as raw JSON text.

        Lets a test send what a JSON object cannot hold (duplicate keys)
        through either protocol era the SDK serves: the handshake era
        (body parsed with pydantic_core.from_json) or the 2026-07-28 era
        (body parsed with json.loads, routing headers and _meta envelope).
        Returns (is_error, structured payload) or raises for an HTTP error.
        """

        meta = ""
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if era == "modern":
            version = "2026-07-28"
            meta = (
                '"_meta":{"io.modelcontextprotocol/protocolVersion":"'
                + version + '","io.modelcontextprotocol/clientCapabilities":{}},'
            )
            headers.update({"Mcp-Method": "tools/call", "Mcp-Name": name})
        else:
            version = "2025-11-25"
        headers["MCP-Protocol-Version"] = version
        body = (
            '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{'
            + meta + '"name":' + json.dumps(name)
            + ',"arguments":' + arguments_text + "}}"
        )
        with httpx2.Client() as http:
            response = http.post(self.url, content=body, headers=headers)
        response.raise_for_status()
        result = response.json()["result"]
        return result.get("isError", False), result["structuredContent"]

    def post(self, headers, body=None):
        """A raw HTTP POST to the MCP endpoint (transport-level checks)."""

        with httpx2.Client() as http:
            return http.post(self.url, headers=headers, json=body or {
                "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            })
