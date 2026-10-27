"""Prepare the frontend's five-chapter index through existing local APIs.

No schema reset, backend edits, direct SQL, real providers, or model calls.
Reuses the agent's existing email (SAT28), leaves its attestation undecided,
and captures real MCP and stale-approval refusals. Only a small public index
is written; credentials stay in this preparation process.
"""

import argparse
import asyncio
import json
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx2 as httpx
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from deploy.demo.bootstrap import CREDENTIALS, read_credentials
from deploy.demo.walkthrough import ATTESTED_REFERENCE, Person, submit, wait_state


AGENT = "demo-north-agent"
AGENT_ID = "5afe0000-0000-4000-8000-000000000105"
NAMES = ("demo-north-requester", "demo-north-investigator", "demo-north-reviewer")


class PreparationRefused(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def local_url(value, *, mcp=False):
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ({"/mcp"} if mcp else {"", "/"})):
        raise argparse.ArgumentTypeError("Only the local HTTP demo is supported")
    return value.rstrip("/")


def credentials(args):
    if args.compose:
        result = subprocess.run(
            ["docker", "compose", "--env-file", ".env", "-f", "deploy/compose.stack.yaml",
             "exec", "-T", "api", "cat", "/state/demo-credentials"],
            cwd=ROOT, capture_output=True, text=True, timeout=20, check=True,
        )
        values = dict(line.split("=", 1) for line in result.stdout.splitlines()
                      if line and not line.startswith("#") and "=" in line)
    else:
        values = read_credentials(args.credentials)
    required = (*NAMES, AGENT)
    if not all(values.get(name) for name in required):
        raise PreparationRefused("The local demo sign-ins or scoped agent key are missing.")
    return {name: values[name] for name in required}


def all_actions(person):
    items = []
    query = {"limit": 100}
    for _ in range(100):
        page = person.get("/actions?" + urlencode(query))
        items.extend(page["items"])
        if not page.get("next_before") or not page.get("next_before_id"):
            return items
        query.update(before=page["next_before"], before_id=page["next_before_id"])
    raise PreparationRefused("The demo has too many actions to select a case safely.")


def find_action(person, summaries, resource, predicate):
    for item in summaries:
        if item["resource_id"] != resource:
            continue
        detail = person.get("/actions/" + item["operation_id"])
        if predicate(detail):
            return detail
    return None


async def capture_agent(mcp_url, key, email_id):
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"}, trust_env=False, timeout=30) as http:
        async with Client(streamable_http_client(mcp_url, http_client=http)) as client:
            if email_id is None:
                result = await client.call_tool("submit_action", {
                    "action": "send_refund_confirmation", "provider_account_id": "mock-email-account",
                    "payload": {"customer_id": "cust_north_3002", "template": "refund_done"},
                    "intent_ref": "guided-email-" + uuid.uuid4().hex[:12],
                })
                if result.is_error:
                    raise PreparationRefused("The agent email could not be admitted. Existing data was preserved; inspect SAT28 rather than resetting automatically.")
                email_id = result.structured_content["operation_id"]
            current = await client.call_tool("get_action_status", {"operation_id": email_id, "wait_seconds": 1})
            if current.is_error:
                raise PreparationRefused("The scoped agent cannot inspect the selected email.")
            result = await client.call_tool("propose_resolution", {
                "operation_id": email_id, "action": "ATTEST_APPLIED",
                "rationale": "I am sure the email was delivered.",
            })
            content = result.structured_content
            if not isinstance(content, dict):
                content = json.loads(" ".join(getattr(part, "text", "") for part in result.content))
            if not result.is_error or content.get("error") != "invalid_arguments":
                raise PreparationRefused("The expected agent authority refusal was not observed.")
            return email_id, {"captured_at": now(), "operation_id": email_id,
                              "tool": "propose_resolution", "action": "ATTEST_APPLIED",
                              "error": content["error"], "message": content["message"]}


def propose(person, operation_id, *, action="ATTEST_APPLIED", reference=None):
    body = {"action": action, "rationale": (
        "Scripted local demo: inspect one more provider observation to demonstrate evidence-bound approval."
        if action == "REVERIFY" else "Scripted local demo: synthetic provider reference supplied by the investigator."
    )}
    if reference:
        body["attested_reference"] = reference
    response = person.post(f"/actions/{operation_id}/proposals", body)
    response.raise_for_status()
    return response.json()


def choose_email_proposal(investigator, reviewer, email):
    proposals = reviewer.get("/proposals?status=all&limit=100")["items"]
    for proposal in proposals:
        if (proposal["operation_id"] == email["operation_id"] and proposal["action"] == "ATTEST_APPLIED"
                and proposal.get("attested_reference") == ATTESTED_REFERENCE):
            if email["state"] == "SUCCEEDED" and proposal["status"] == "approved":
                return proposal
            expires = datetime.fromisoformat(proposal["expires_at"].replace("Z", "+00:00"))
            # The API permits only one undecided proposal per action/version.
            # Reuse it until expiry; do not approve or reject it for the viewer.
            if proposal["status"] == "open" and expires > datetime.now(timezone.utc):
                return proposal
    if email["state"] != "ESCALATED_UNRESOLVED":
        raise PreparationRefused("The email already changed and has no matching usable attestation proposal. No email was resent.")
    return propose(investigator, email["operation_id"], reference=ATTESTED_REFERENCE)


def capture_stale(investigator, reviewer, order):
    # A refusal already captured for this stack stays a recorded event after
    # its proposal expires. Do not request another lookup just to open the UI.
    index = ROOT / "ui/public/demo-cases.json"
    if index.exists():
        try:
            saved = json.loads(index.read_text(encoding="utf-8"))
            entry = saved["order"]
            trace = saved["stale_refusal"]
            if (saved["schema_version"] == 1 and entry["operation_id"] == order["operation_id"]
                    and trace["proposal_id"] == entry["proposal_id"] and trace["http_status"] == 409
                    and trace["code"] == "stale_approval"
                    and trace["reason"] in {"evidence_changed", "operation_changed"}):
                proposal = reviewer.get("/proposals/" + entry["proposal_id"])
                if (proposal["operation_id"] == order["operation_id"] and proposal["action"] == "ATTEST_APPLIED"
                        and not proposal["decision"]
                        and (proposal["current"]["evidence_digest"] != proposal["binding"]["evidence_digest"]
                             or proposal["current"]["seq"] != proposal["binding"]["seq"])):
                    print("Reusing the existing recorded stale-approval refusal.", flush=True)
                    return entry["proposal_id"], trace
        except (KeyError, TypeError, ValueError, httpx.HTTPStatusError):
            pass
    # Request one permitted read through the normal reviewer path; never
    # manufacture observations or change the order's outcome directly.
    attestation = propose(investigator, order["operation_id"], reference="crm_order_seen_1")
    check = propose(investigator, order["operation_id"], action="REVERIFY")
    response = reviewer.post(f"/proposals/{check['proposal_id']}/decision", {
        "decision": "approve", "binding_digest": check["binding_digest"], "decision_key": str(uuid.uuid4()),
    })
    response.raise_for_status()
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        current = reviewer.get("/proposals/" + attestation["proposal_id"])
        if (current["current"]["evidence_digest"] != current["binding"]["evidence_digest"]
                or current["current"]["seq"] != current["binding"]["seq"]):
            break
        time.sleep(0.5)
    else:
        raise PreparationRefused("No changed order evidence was observed. A stale refusal was not invented.")
    response = reviewer.post(f"/proposals/{attestation['proposal_id']}/decision", {
        "decision": "approve", "binding_digest": attestation["binding_digest"], "decision_key": str(uuid.uuid4()),
    })
    body = response.json()
    error = body.get("error") or {}
    if (response.status_code != 409 or error.get("code") != "stale_approval"
            or error.get("reason") not in {"evidence_changed", "operation_changed"}):
        raise PreparationRefused("The expected stale-evidence approval refusal was not observed.")
    return attestation["proposal_id"], {"captured_at": now(), "proposal_id": attestation["proposal_id"],
        "http_status": response.status_code, "code": error["code"], "reason": error["reason"]}


def prepare(args):
    values = credentials(args)
    people = []
    try:
        for name in NAMES:
            person = Person(args.api, name, values[name])
            people.append(person)
            principal = person.get("/auth/me")
            if principal.get("demo") is not True or principal.get("kind") != "human":
                raise PreparationRefused("Only synthetic human demo accounts may prepare this walkthrough.")
        requester, investigator, reviewer = people
        print("Inspecting existing mock cases; no reset will run.", flush=True)
        summaries = all_actions(reviewer)
        refund = find_action(reviewer, summaries, "ch_north_1002", lambda op:
            op["connector_id"] == "mock-payments" and op["action_type"] == "refund_payment"
            and op["payload"].get("amount_minor") == 245000 and op["state"] == "SUCCEEDED"
            and op.get("success_provenance") == "provider_verified" and len(op["attempts"]) == 1
            and any(t["to_state"] == "UNKNOWN" for t in op["transitions"]))
        if refund is None:
            ident = submit(requester, "refund_payment", "mock-payments-account", {"charge_id": "ch_north_1002", "amount_minor": 245000})
            refund = wait_state(reviewer, ident, "SUCCEEDED", 90)
        if (refund["state"] != "SUCCEEDED" or refund.get("success_provenance") != "provider_verified"
                or len(refund["attempts"]) != 1 or not any(t["to_state"] == "UNKNOWN" for t in refund["transitions"])):
            raise PreparationRefused("The refund did not demonstrate the expected verified one-attempt recovery.")
        email = find_action(reviewer, summaries, "cust_north_3002", lambda op:
            op["connector_id"] == "mock-email" and op["action_type"] == "send_refund_confirmation"
            and op.get("requested_by") == AGENT_ID)
        print("Capturing the agent's real MCP attestation refusal.", flush=True)
        email_id, agent_refusal = asyncio.run(capture_agent(args.mcp, values[AGENT], email["operation_id"] if email else None))
        email = wait_state(reviewer, email_id, {"ESCALATED_UNRESOLVED", "SUCCEEDED"}, 90)
        if len(email["attempts"]) != 1 or not any(t["to_state"] == "UNKNOWN" for t in email["transitions"]):
            raise PreparationRefused("The selected email does not demonstrate a lost-response single attempt.")
        order = find_action(reviewer, summaries, "case_north_2002", lambda op:
            op["connector_id"] == "mock-crm" and op["action_type"] == "create_replacement_order"
            and op["state"] == "ESCALATED_UNRESOLVED" and op.get("workflow") is None)
        if order is None:
            ident = submit(requester, "create_replacement_order", "mock-crm-account", {"case_id": "case_north_2002", "sku": "sku-replacement"})
            order = wait_state(reviewer, ident, "ESCALATED_UNRESOLVED", 120)
        if order["state"] != "ESCALATED_UNRESOLVED":
            raise PreparationRefused("The standalone order did not remain unresolved.")
        print("Capturing stale approval through an approved provider lookup.", flush=True)
        order_proposal, stale_refusal = capture_stale(investigator, reviewer, order)
        email_proposal = choose_email_proposal(investigator, reviewer, email)
        result = {"schema_version": 1, "prepared_at": now(),
                  "refund": {"operation_id": refund["operation_id"]},
                  "email": {"operation_id": email_id, "proposal_id": email_proposal["proposal_id"]},
                  "order": {"operation_id": order["operation_id"], "proposal_id": order_proposal},
                  "agent_refusal": agent_refusal, "stale_refusal": stale_refusal}
        target = ROOT / "ui/public/demo-cases.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)
        print("Prepared ui/public/demo-cases.json. No credentials were written. Email attestation was not approved by this tool.", flush=True)
        if email_proposal["status"] == "approved":
            print("The email was already approved in this demo; the walkthrough will show its recorded decision, not pretend it is new.")
    finally:
        for person in people:
            person.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", type=local_url, default="http://127.0.0.1:8080")
    parser.add_argument("--mcp", type=lambda value: local_url(value, mcp=True), default="http://127.0.0.1:8002/mcp")
    parser.add_argument("--compose", action="store_true", help="read only the existing Compose demo credentials")
    parser.add_argument("--credentials", type=Path, default=CREDENTIALS)
    args = parser.parse_args()
    try:
        prepare(args)
    except PreparationRefused as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        # Client exceptions can include request data. Never print credentials.
        print(f"Preparation stopped ({type(error).__name__}). Existing data was preserved; check the local stack and sign-ins.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
