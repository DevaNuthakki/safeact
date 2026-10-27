"""Fresh-clone smoke test, run inside the Compose stack (DEMO ONLY).

    docker compose --env-file .env -f deploy/compose.stack.yaml \
        --profile check run --rm smoke

Through nginx, as a browser would:
1. sign in as the demo requester and take the change feed's horizon;
2. submit a refund;
3. follow the feed (SSE, reconnecting with Last-Event-ID) until it names the
   operation and the REST detail says SUCCEEDED;
4. a console page loads on an API path (Accept: text/html).
Then, inside the stack: every component's /metrics target is up in
Prometheus, and the MCP endpoint lists its tools for a demo agent key.
Prints a JSON summary without any secret; exits 1 on any failure.
"""

import asyncio
import json
import sys
import time
import uuid

import httpx2 as httpx

from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from api.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from deploy.demo.bootstrap import read_credentials


CONSOLE = "http://127.0.0.1:8080"
MCP = "http://127.0.0.1:8002/mcp"
PROMETHEUS = "http://127.0.0.1:9090"
CREDENTIALS = "/state/demo-credentials"
REQUESTER = "demo-north-requester"
AGENT = "demo-north-agent"
COMPONENTS = {"api", "mcp", "worker", "relay", "reconciler", "advancer"}


def frames(lines):
    """Parse SSE lines into (event, id, data) frames as they arrive."""
    event = event_id = data = None
    for line in lines:
        if line == "":
            if event is not None:
                yield event, event_id, json.loads(data or "{}")
            event = event_id = data = None
        elif line.startswith(":"):
            continue
        else:
            name, _, value = line.partition(": ")
            if name == "event":
                event = value
            elif name == "id":
                event_id = value
            elif name == "data":
                data = value


def sign_in(password):
    http = httpx.Client(base_url=CONSOLE, timeout=10, trust_env=False,
                        headers={"Accept": "application/json"})
    response = http.post("/auth/login", json={"username": REQUESTER,
                                              "password": password})
    response.raise_for_status()
    # A browser sends Secure cookies to localhost; httpx does not over http.
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        http.cookies.set(name, response.cookies[name])
    http.headers[CSRF_HEADER] = response.json()["csrf_token"]
    return http


def horizon(http):
    with http.stream("GET", "/events", timeout=httpx.Timeout(10, read=30)) as response:
        response.raise_for_status()
        for event, event_id, _ in frames(response.iter_lines()):
            if event == "reset":
                return event_id
    raise RuntimeError("the change feed sent no horizon")


def follow(http, operation_id, cursor, timeout=90):
    """Follow the feed until it names the operation and REST says SUCCEEDED."""
    seen = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        headers = {"Last-Event-ID": cursor} if cursor else {}
        with http.stream("GET", "/events", headers=headers,
                         timeout=httpx.Timeout(10, read=30)) as response:
            response.raise_for_status()
            for event, event_id, data in frames(response.iter_lines()):
                cursor = event_id or cursor
                if event == "changes":
                    seen |= operation_id in {c["operation_id"] for c in data["changes"]}
                    detail = http.get(f"/actions/{operation_id}").json()
                    if seen and detail["state"] == "SUCCEEDED":
                        return detail
                if event in ("reconnect", "session_ended"):
                    break
    raise RuntimeError("the change feed never showed the refund succeeding")


async def mcp_tools(key):
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"},
                                 trust_env=False, timeout=10) as http:
        async with Client(streamable_http_client(MCP, http_client=http)) as client:
            return sorted(tool.name for tool in (await client.list_tools()).tools)


def metrics_targets(timeout=60):
    """Prometheus scrapes every 5 s; wait until each target has been scraped."""
    deadline = time.monotonic() + timeout
    while True:
        targets = httpx.get(PROMETHEUS + "/api/v1/targets", timeout=10,
                            trust_env=False).json()["data"]["activeTargets"]
        health = {t["labels"]["component"]: t["health"] for t in targets}
        if set(health.values()) == {"up"} or time.monotonic() > deadline:
            return health
        time.sleep(2)


def main():
    values = read_credentials(CREDENTIALS)
    summary = {}
    http = sign_in(values[REQUESTER])
    try:
        start = horizon(http)
        created = http.post("/actions", json={
            "request_key": "smoke-" + uuid.uuid4().hex[:12],
            "action_type": "refund_payment",
            "provider_account_id": "mock-payments-account",
            "payload": {"charge_id": "ch_north_1001", "amount_minor": 1500},
        })
        created.raise_for_status()
        operation_id = created.json()["operation_id"]
        detail = follow(http, operation_id, start)
        summary["refund"] = {"state": detail["state"],
                             "seen_in_change_feed": True}
        page = http.get(f"/actions/{operation_id}", headers={"Accept": "text/html"})
        summary["console_page"] = page.status_code == 200 and '<div id="root">' in page.text
    finally:
        http.close()
    summary["metrics_targets"] = metrics_targets()
    summary["mcp_tools"] = asyncio.run(mcp_tools(values[AGENT]))

    ok = (summary["refund"]["state"] == "SUCCEEDED" and summary["console_page"]
          and set(summary["metrics_targets"]) == COMPONENTS
          and set(summary["metrics_targets"].values()) == {"up"}
          and bool(summary["mcp_tools"]))
    summary["result"] = "PASS" if ok else "FAIL"
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
