"""Seed the three-minute demo's cases on a running demo stack (DEMO ONLY).

    .venv/bin/python -m deploy.demo.walkthrough            # dev stack (run_stack)
    .venv/bin/python -m deploy.demo.walkthrough --show-stale
    .venv/bin/python -m deploy.demo.walkthrough --api http://localhost:8080 \
        --credentials /path/to/demo-credentials           # Compose stack

Uses only what any client may: the API as the synthetic demo people and the
guarded MCP server as the demo agent, with the keys in the demo credentials
file. The faults come from the demo's fixed script (deploy/demo/run_stack.py),
keyed by resource id. docs/demo-walkthrough.md says what to show for each.

1. Timeout after success: a $2,450.00 refund for a damaged order on
   ``ch_north_1002``. The provider applies it and the response is lost; a
   naive retry would refund it twice. SafeAct records UNKNOWN, verifies by
   lookup and ends SUCCEEDED with one attempt (no second dispatch).
2. Late commit, empty lookup: replacement order ``case_north_2002``. The order
   exists but search cannot see it, so SafeAct escalates instead of guessing.
3. Agent: the demo agent lists its MCP tools (no approval tool), submits the
   refund confirmation email ``cust_north_3002`` (sent, response lost: escalated
   at once, never resent), and its attempt to attest the email as applied is
   refused: agents may propose only REVERIFY or PROTECTED_RETRY.
4. Human evidence path: the investigator proposes ATTEST_APPLIED for the email
   with the provider reference; a different human reviewer decides in the
   console. The investigator also proposes ATTEST_APPLIED for the escalated
   order: the reconciler's next post-escalation lookup makes that proposal
   stale, and approving it is refused (``--show-stale`` does that here and
   prints the 409).
"""

import argparse
import asyncio
import json
import sys
import time
import uuid

from pathlib import Path

import httpx2 as httpx

from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from api.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from deploy.demo.bootstrap import CREDENTIALS, read_credentials


TENANT = "north"
REQUESTER = f"demo-{TENANT}-requester"
INVESTIGATOR = f"demo-{TENANT}-investigator"
REVIEWER = f"demo-{TENANT}-reviewer"
AGENT = f"demo-{TENANT}-agent"
ATTESTED_REFERENCE = "msg_demo_delivered_1"
# Beat 1: a realistic amount, so a duplicate would visibly cost $2,450.00.
# The mocks model no charge balance; only the catalog's cap applies.
DAMAGED_ORDER_REFUND = 245_000  # cents


class SeedRefused(RuntimeError):
    """The demo cannot be seeded as it stands (the message says why)."""


class Person:
    """One signed-in console user, over plain HTTP like the console."""

    def __init__(self, api, username, password):
        self.http = httpx.Client(base_url=api, timeout=15, trust_env=False,
                                 headers={"Accept": "application/json"})
        response = self.http.post("/auth/login", json={"username": username,
                                                       "password": password})
        response.raise_for_status()
        # A browser sends Secure cookies to localhost; httpx does not over http.
        for name in (SESSION_COOKIE, CSRF_COOKIE):
            self.http.cookies.set(name, response.cookies[name])
        self.http.headers[CSRF_HEADER] = response.json()["csrf_token"]

    def get(self, path):
        response = self.http.get(path)
        response.raise_for_status()
        return response.json()

    def post(self, path, body):
        return self.http.post(path, json=body)

    def close(self):
        self.http.close()


def submit(person, action, account, payload):
    response = person.post("/actions", {
        "request_key": "walkthrough-" + uuid.uuid4().hex[:16],
        "action_type": action, "provider_account_id": account, "payload": payload,
    })
    response.raise_for_status()
    return response.json()["operation_id"]


def wait_state(person, operation_id, states, timeout):
    states = {states} if isinstance(states, str) else set(states)
    deadline = time.monotonic() + timeout
    while True:
        detail = person.get(f"/actions/{operation_id}")
        if detail["state"] in states or time.monotonic() > deadline:
            return detail
        time.sleep(0.5)


async def agent_steps(mcp_url, key):
    """The agent's beat: tools, its own email, a refused self-attestation."""
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"},
                                 trust_env=False, timeout=30) as http:
        async with Client(streamable_http_client(mcp_url, http_client=http)) as client:
            tools = sorted(t.name for t in (await client.list_tools()).tools)
            submitted = await client.call_tool("submit_action", {
                "action": "send_refund_confirmation",
                "provider_account_id": "mock-email-account",
                "payload": {"customer_id": f"cust_{TENANT}_3002", "template": "refund_done"},
                "intent_ref": "walkthrough-email-" + uuid.uuid4().hex[:8],
            })
            if submitted.is_error:
                return {"refused": submitted.structured_content.get("error")}
            operation_id = submitted.structured_content["operation_id"]
            status = await client.call_tool("get_action_status", {
                "operation_id": operation_id, "wait_seconds": 10})
            attest = await client.call_tool("propose_resolution", {
                "operation_id": operation_id, "action": "ATTEST_APPLIED",
                "rationale": "I am sure the email was delivered."})
            refusal = " ".join(getattr(part, "text", "") for part in attest.content)
            return {
                "tools": tools,
                "approval_tool_offered": any("approv" in name for name in tools),
                "email_operation_id": operation_id,
                "email_state_seen_by_agent": status.structured_content["state"],
                "attest_applied_refused": attest.is_error,
                "refusal": refusal[:200],
            }


def show_stale(reviewer, proposal, timeout):
    """Wait until the proposal is stale, then approve with the old binding."""
    deadline = time.monotonic() + timeout
    status = None
    while time.monotonic() < deadline:
        status = reviewer.get(f"/proposals/{proposal['proposal_id']}")["status"]
        if status == "stale":
            break
        time.sleep(1)
    response = reviewer.post(f"/proposals/{proposal['proposal_id']}/decision", {
        "decision": "approve", "binding_digest": proposal["binding_digest"],
        "decision_key": str(uuid.uuid4()),
    })
    body = response.json()
    return {"status_before": status, "http_status": response.status_code,
            "code": (body.get("error") or {}).get("code"),
            "reason": (body.get("error") or {}).get("reason")}


def seed(api, mcp_url, values, *, console=None, stale=False, timeout=180):
    console = (console or api).rstrip("/")
    link = lambda kind, ident: f"{console}/{kind}/{ident}"
    people = {name: Person(api, name, values[name])
              for name in (REQUESTER, INVESTIGATOR, REVIEWER)}
    result = {}
    try:
        requester, investigator = people[REQUESTER], people[INVESTIGATOR]

        refund = submit(requester, "refund_payment", "mock-payments-account",
                        {"charge_id": f"ch_{TENANT}_1002", "amount_minor": DAMAGED_ORDER_REFUND})
        order = submit(requester, "create_replacement_order", "mock-crm-account",
                       {"case_id": f"case_{TENANT}_2002", "sku": "sku-replacement"})
        detail = wait_state(requester, refund, "SUCCEEDED", timeout)
        result["1_timeout_after_success"] = {
            "operation": link("actions", refund), "state": detail["state"],
            "path": [t["to_state"] for t in detail["transitions"]],
            "attempts": len(detail["attempts"]),
        }

        result["3_agent"] = asyncio.run(agent_steps(mcp_url, values[AGENT]))
        if "refused" in result["3_agent"]:
            # SAT28: an agent gets one operation per target unless every
            # earlier one ended FAILED_CONFIRMED or CANCELLED_BEFORE_CLAIM.
            raise SeedRefused(
                "The demo agent was refused a new email to "
                f"cust_{TENANT}_3002 ({result['3_agent']['refused']}): an agent gets "
                "one operation per target, so this beat runs once per demo. Rebuild "
                "the demo (python -m deploy.demo.bootstrap, or for Compose `down -v` "
                "and `up --wait`) and seed again.")
        email = result["3_agent"]["email_operation_id"]
        result["3_agent"]["email_operation"] = link("actions", email)

        detail = wait_state(requester, order, "ESCALATED_UNRESOLVED", timeout)
        result["2_late_commit_empty_lookup"] = {
            "operation": link("actions", order), "state": detail["state"],
            "path": [t["to_state"] for t in detail["transitions"]],
            "attempts": len(detail["attempts"]),
        }

        wait_state(investigator, email, "ESCALATED_UNRESOLVED", timeout)
        permitted = investigator.post(f"/actions/{email}/proposals", {
            "action": "ATTEST_APPLIED", "attested_reference": ATTESTED_REFERENCE,
            "rationale": "The email provider's dashboard shows the message delivered.",
        })
        permitted.raise_for_status()
        stale_one = investigator.post(f"/actions/{order}/proposals", {
            "action": "ATTEST_APPLIED", "attested_reference": "crm_order_seen_1",
            "rationale": "The order is visible in the CRM console.",
        })
        stale_one.raise_for_status()
        result["4_human_review"] = {
            "decide_in_console_as": REVIEWER,
            "approve_this": link("proposals", permitted.json()["proposal_id"]),
            "goes_stale": link("proposals", stale_one.json()["proposal_id"]),
        }
        if stale:
            result["4_human_review"]["stale_approval"] = show_stale(
                people[REVIEWER], stale_one.json(), timeout)
    finally:
        for person in people.values():
            person.close()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://127.0.0.1:8000",
                        help="API base (the Compose console http://localhost:8080 also serves it)")
    parser.add_argument("--mcp", default="http://127.0.0.1:8002/mcp")
    parser.add_argument("--console", default=None,
                        help="console base for printed links (default: http://127.0.0.1:5173 "
                             "for the dev API, else the API base)")
    parser.add_argument("--credentials", type=Path, default=CREDENTIALS)
    parser.add_argument("--show-stale", action="store_true",
                        help="wait for the order's proposal to go stale and show the refusal")
    args = parser.parse_args(argv)
    console = args.console or ("http://127.0.0.1:5173" if args.api.endswith(":8000")
                               else args.api)
    try:
        result = seed(args.api, args.mcp, read_credentials(args.credentials),
                      console=console, stale=args.show_stale)
    except SeedRefused as refused:
        print(refused, file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    ok = (result["1_timeout_after_success"]["state"] == "SUCCEEDED"
          and result["1_timeout_after_success"]["attempts"] == 1
          and result["2_late_commit_empty_lookup"]["state"] == "ESCALATED_UNRESOLVED"
          and not result["3_agent"]["approval_tool_offered"]
          and result["3_agent"]["attest_applied_refused"])
    if args.show_stale:
        ok = ok and result["4_human_review"]["stale_approval"]["http_status"] == 409
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
