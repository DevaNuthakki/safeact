"""Submit and follow one replacement through the existing local demo API.

    .venv/bin/python -m examples.replacement --request-key demo-replacement-001

Uses synthetic demo accounts and mock providers only. The caller retains the
request key; rerunning the same command after a lost reply recovers the same
workflow. There is no direct provider call or automatic new business intent.
"""

import argparse
import getpass
import json
import time
from urllib.parse import urlsplit

import httpx2

from api.actions import CreateWorkflow
from api.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE


SETTLED = {"COMPLETED", "FAILED_NO_EFFECT", "COMPENSATED_VERIFIED",
           "UNRESOLVED", "COMPENSATION_UNRESOLVED"}
MESSAGES = {
    "COMPLETED": "Charge and order confirmed by recorded evidence.",
    "FAILED_NO_EFFECT": "Charge confirmed not applied; no order started.",
    "COMPENSATED_VERIFIED": "Refund confirmed by recorded evidence; replacement not completed.",
    "UNRESOLVED": "Investigate the uncertain step; an unknown order must not trigger a refund.",
    "COMPENSATION_UNRESOLVED": "Investigate the refund; do not create another refund while uncertain.",
}


def local_url(value):
    parts = urlsplit(value)
    if (parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost"}
            or parts.username or parts.password or parts.path not in {"", "/"}
            or parts.query or parts.fragment):
        raise argparse.ArgumentTypeError("Use the local HTTP demo origin (localhost or 127.0.0.1).")
    return value.rstrip("/")


def sign_in(client, username, password):
    response = client.post("/auth/login", json={"username": username, "password": password})
    response.raise_for_status()
    principal = response.json()["principal"]
    if not principal.get("demo"):
        raise ValueError("This example requires a demo account.")
    # The API's Secure cookie flags stay unchanged. httpx does not have the
    # browser's localhost exception; carry cookies explicitly only to the
    # loopback-only client. Nothing is saved or printed.
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        client.cookies.set(name, response.cookies[name])
    client.headers[CSRF_HEADER] = response.json()["csrf_token"]


def follow(client, request, *, wait_seconds=75, clock=time.monotonic, sleep=time.sleep):
    admitted = client.post("/workflows", json=request)
    admitted.raise_for_status()
    workflow_id = admitted.json()["workflow_id"]
    deadline = clock() + wait_seconds
    while True:
        response = client.get(f"/workflows/{workflow_id}")
        response.raise_for_status()
        workflow = response.json()
        status = workflow["status"]
        if status in SETTLED or clock() >= deadline:
            return {
                "workflow_id": workflow_id, "request_key": request["request_key"],
                "status": status,
                "message": MESSAGES.get(status, "Still processing. Follow the existing workflow; do not submit a new intent."),
                "steps": workflow["steps"],
                "evidence_path": f"/workflows/{workflow_id}",
            }
        sleep(min(1, max(0, deadline - clock())))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", type=local_url, default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="demo-north-requester")
    parser.add_argument("--request-key", required=True, help="retain this key and reuse the exact arguments after uncertainty")
    parser.add_argument("--customer", default="cust_north_3001")
    parser.add_argument("--case", default="case_north_2001")
    parser.add_argument("--sku", default="sku-replacement")
    parser.add_argument("--fee-minor", type=int, default=500)
    parser.add_argument("--wait-seconds", type=float, default=75)
    args = parser.parse_args(argv)
    if not 0 <= args.wait_seconds <= 300:
        parser.error("--wait-seconds must be between 0 and 300")
    try:
        request = CreateWorkflow(
            request_key=args.request_key, customer_id=args.customer, case_id=args.case,
            sku=args.sku, fee_minor=args.fee_minor, payments_account_id="mock-payments-account",
            crm_account_id="mock-crm-account",
        ).model_dump()
    except ValueError:
        parser.error("Invalid request key, identifiers or fee; see the local API schema.")
    password = getpass.getpass("Demo password (from your local .demo-credentials): ")
    try:
        with httpx2.Client(base_url=args.base_url, timeout=10, trust_env=False,
                           follow_redirects=False) as client:
            sign_in(client, args.username, password)
            result = follow(client, request, wait_seconds=args.wait_seconds)
    except httpx2.HTTPStatusError as error:
        print(f"API refused the request (HTTP {error.response.status_code}).")
        if error.response.status_code == 409:
            print("Resolve the existing request or payload conflict before creating another intent.")
        return 2
    except httpx2.TransportError:
        print("Connection interrupted; admission or execution may already have happened.")
        print("Rerun with the SAME request key and exact arguments. Do not generate a new key.")
        return 3
    except (ValueError, KeyError, TypeError):
        print("The server did not return a valid demo response.")
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in {"COMPLETED", "COMPENSATED_VERIFIED"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
