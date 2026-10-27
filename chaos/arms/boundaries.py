"""Real human API and MCP qualification drivers; mock providers only.

Credentials never enter observations. All effects and review state are read
independently; the harness never fabricates lifecycle transitions or evidence.
"""

import asyncio
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx2
import psycopg
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from psycopg import sql

from api.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from chaos.arms.base import Admission
from connectors.http_trace import correlation_hash
from storage.database import local_admin_settings
from storage.identity_admin import issue_agent_key, revoke_agent_key


HUMANS = ("requester", "investigator", "reviewer", "reviewer2")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class SafeActBoundaries:

    def _boundary_event(self, event, **fields):
        row = dict(source=fields.pop("source", "boundary_client"), event=event,
                   monotonic_ns=time.monotonic_ns(), **fields)
        with self._boundary_lock:
            self.boundary_events.append(row)
        return row

    def prepare_boundary(self, scenario):
        self.boundary_events = []
        self._boundary_lock = threading.Lock()
        if scenario.family not in ("F14", "F15"):
            return
        self._boundary_scenario = scenario
        self._boundary_copy = 0
        s = sql.Identifier(self.env.schema)
        self._actors = dict(self.admin.execute(sql.SQL(
            "SELECT username, principal_id FROM {}.safeact_principals"
        ).format(s)).fetchall())
        actor = "demo-north-" + ("requester" if scenario.family == "F14" else "agent")
        with self.admin.transaction():
            for operation in scenario.operations:
                self.admin.execute(sql.SQL(
                    "INSERT INTO {}.safeact_action_grants (principal_id, connector_id, "
                    "provider_account_id, action_type, resource_id, contract_version) "
                    "VALUES (%s, 'mock-crm', %s, 'create_replacement_order', %s, %s)"
                ).format(s), (self._actors[actor], self.account(scenario),
                              operation.resource_id, scenario.contract_version))
            if scenario.family == "F14" and scenario.review_mode == "protected_retry":
                self.admin.execute(sql.SQL(
                    "INSERT INTO {}.safeact_contract_review_policies "
                    "(connector_id, version, retry_requires_approval) "
                    "VALUES ('mock-crm', %s, true)"
                ).format(s), (scenario.contract_version,))
            if scenario.family == "F15":
                self._agent_admission_barrier = threading.Barrier(scenario.operations[0].submits, timeout=10)
                self._agent_keys = [issue_agent_key(self.admin, self.env.schema,
                    principal_id=self._actors["demo-north-agent"],
                    scopes=("submit", "investigate"), label=f"qualification-{i}",
                    created_by="qualification") for i in range(2)]
                self._south_key = issue_agent_key(self.admin, self.env.schema,
                    principal_id=self._actors["demo-south-agent"],
                    scopes=("submit", "investigate"), label="qualification-south",
                    created_by="qualification")
        # Fixed ports are protected by the qualification slot's exclusive lock.
        self.stack.api_port = 21400 + self.slot.index * 10
        self.stack.mcp_port = self.stack.api_port + 1
        extra = ("api", "mcp") if scenario.family == "F15" else ("api",)
        self.stack.components = (*self.stack.components, *extra)
        for component in extra:
            self.stack.spawn(component)
        self.stack._wait_http(self.stack.base_url + "/healthz", 30)
        if "mcp" in extra:
            self.stack._wait_port(self.stack.mcp_port, 30)
        self._human_headers = {}
        if scenario.family == "F14":
            for human in HUMANS:
                username = "demo-north-" + human
                with self._http() as client:
                    response = client.post("/auth/login", json={
                        "username": username, "password": self.account_passwords[username]})
                    require(response.status_code == 200, "Qualification human sign-in failed")
                    self._human_headers[human] = {
                        "Cookie": f"{SESSION_COOKIE}={response.cookies[SESSION_COOKIE]}; "
                                  f"{CSRF_COOKIE}={response.cookies[CSRF_COOKIE]}",
                        CSRF_HEADER: response.json()["csrf_token"],
                    }
        else:
            # Official protocol initialization before the observation window.
            for key in (*self._agent_keys, self._south_key):
                asyncio.run(self._mcp(key, None, {}))

    def _http(self):
        return httpx2.Client(base_url=self.stack.base_url, trust_env=False,
                             follow_redirects=False, timeout=15)

    def _human(self, who, method, path, body=None, *, label):
        started = time.monotonic_ns()
        with self._http() as client:
            response = client.request(method, path, json=body,
                                      headers=self._human_headers[who])
        payload = response.json()
        self._boundary_event(label, actor_id=str(self._actors["demo-north-" + who]),
            sample_started_ns=started, sample_finished_ns=time.monotonic_ns(),
            method=method, status_code=response.status_code,
            operation_id=payload.get("operation_id"), proposal_id=(
                payload.get("proposal_id") or (path.split("/")[2] if path.startswith("/proposals/") else None)),
            outcome=payload.get("outcome"), error=payload.get("error", {}).get("code"),
            binding_digest=body.get("binding_digest") if body else payload.get("binding_digest"),
            decision_key=body.get("decision_key") if body else None)
        return response.status_code, payload

    async def _mcp(self, key, name, arguments, *, rendezvous=False):
        try:
            async with httpx2.AsyncClient(trust_env=False, follow_redirects=False,
                    timeout=15, headers={"Authorization": f"Bearer {key.key}"}) as http:
                async with Client(streamable_http_client(
                        f"http://127.0.0.1:{self.stack.mcp_port}/mcp", http_client=http)) as client:
                    if name is None:
                        return [tool.name for tool in (await client.list_tools()).tools]
                    if rendezvous:
                        self._agent_admission_barrier.wait()
                    started = time.monotonic_ns()
                    result = await client.call_tool(name, arguments)
                    return result.is_error, result.structured_content, started, time.monotonic_ns()
        except Exception as error:
            # Do not expose transport objects, headers or credential-bearing URLs.
            raise RuntimeError(f"Qualification MCP transport failed: {type(error).__name__}") from None

    def _agent(self, key, name, arguments, *, label):
        error, payload, started, finished = asyncio.run(self._mcp(
            key, name, arguments, rendezvous=label == "agent_admission"))
        require(isinstance(payload, dict), "MCP returned no structured result")
        self._boundary_event(label, key_id=str(key.key_id),
            principal_id=str(self._actors["demo-south-agent" if key is self._south_key else "demo-north-agent"]),
            tool=name, sample_started_ns=started, sample_finished_ns=finished,
            is_error=error, error=payload.get("error"),
            operation_id=payload.get("operation_id"), proposal_id=payload.get("proposal_id"))
        return error, payload

    def _agent_arguments(self, scenario, operation, *, changed=False):
        return dict(action="create_replacement_order", provider_account_id=self.account(scenario),
            payload=dict(case_id=operation.resource_id, sku=f"sku_{operation.amount_minor + int(changed)}"),
            intent_ref=f"{scenario.scenario_id}-0", contract_version=scenario.contract_version)

    def boundary_submit(self, scenario, index, operation, *, changed=False):
        if scenario.family == "F14":
            status, body = self._human("requester", "POST", "/actions", dict(
                action_type="create_replacement_order", request_key=f"{scenario.scenario_id}-{index}",
                provider_account_id=self.account(scenario), contract_version=scenario.contract_version,
                payload=dict(case_id=operation.resource_id, sku=f"sku_{operation.amount_minor + int(changed)}")),
                label="human_admission")
            require(status in (200, 201), "Authorized qualification API admission was refused")
        else:
            with self._boundary_lock:
                key = self._agent_keys[self._boundary_copy % 2]
                self._boundary_copy += 1
            error, body = self._agent(key, "submit_action",
                self._agent_arguments(scenario, operation, changed=changed), label="agent_admission")
            require(not error, "Authorized qualification MCP admission was refused")
        local_id = body["operation_id"]
        with self._broker_ids_lock:
            self._broker_ids.add(local_id)
        return Admission(local_id)

    def _snapshot(self, local_id, label):
        """A separate repeatable-read, read-only connection sees committed rows."""
        s = sql.Identifier(self.env.schema)
        started = time.monotonic_ns()
        with psycopg.connect(**local_admin_settings()) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            row = connection.execute(sql.SQL(
                "SELECT p.state, p.seq, {}.safeact_api_requested_by(o.operation_id), o.connector_id, "
                "coalesce(o.provider_idempotency_key, o.operation_id::text) "
                "FROM {}.safeact_state_projection p JOIN {}.safeact_operations o USING(operation_id) "
                "WHERE p.operation_id = %s"
            ).format(s, s, s), (local_id,)).fetchone()
            require(row is not None, "Boundary observer lost an admitted operation")
            proposals = connection.execute(sql.SQL(
                "SELECT proposal_id, proposed_by, action, bound_seq, bound_state, binding_digest "
                "FROM {}.safeact_proposals WHERE operation_id = %s ORDER BY proposal_id"
            ).format(s), (local_id,)).fetchall()
            decisions = connection.execute(sql.SQL(
                "SELECT d.proposal_id, d.decided_by, d.decision, d.decision_key "
                "FROM {}.safeact_decisions d JOIN {}.safeact_proposals p USING(proposal_id) "
                "WHERE p.operation_id = %s ORDER BY d.proposal_id"
            ).format(s, s), (local_id,)).fetchall()
            evidence = connection.execute(sql.SQL(
                "SELECT outcome_id, outcome, evidence_kind, provenance FROM {}.safeact_attempt_outcomes "
                "WHERE operation_id = %s ORDER BY outcome_id"
            ).format(s), (local_id,)).fetchall()
            audit = connection.execute(sql.SQL(
                "SELECT audit_id, event_type, actor_id, commit_xid::text, details "
                "FROM {}.safeact_audit_events WHERE operation_id = %s AND event_type IN "
                "('PROPOSAL_RECORDED', 'DECISION_RECORDED', 'APPROVAL_REFUSED_STALE') ORDER BY audit_id"
            ).format(s), (local_id,)).fetchall()
            operation_ids = [str(r[0]) for r in connection.execute(sql.SQL(
                "SELECT operation_id FROM {}.safeact_operations WHERE resource_id = %s ORDER BY operation_id"
            ).format(s), (self._boundary_scenario.operations[0].resource_id,)).fetchall()]
            principals = connection.execute(sql.SQL(
                "SELECT p.principal_id, p.kind, p.tenant_id, array_agg(g.role ORDER BY g.role) "
                "FROM {}.safeact_principals p JOIN {}.safeact_role_grants g USING(principal_id) "
                "GROUP BY p.principal_id, p.kind, p.tenant_id ORDER BY p.principal_id"
            ).format(s, s)).fetchall()
            keys = connection.execute(sql.SQL(
                "SELECT k.key_id, k.principal_id, k.scopes, r.key_id IS NOT NULL "
                "FROM {}.safeact_agent_keys k LEFT JOIN {}.safeact_agent_key_revocations r USING(key_id) "
                "ORDER BY k.key_id"
            ).format(s, s)).fetchall()
        finished = time.monotonic_ns()
        sample = dict(source="database", event="db_boundary_snapshot", label=label,
            operation_id=local_id, sample_started_ns=started, sample_finished_ns=finished,
            monotonic_ns=finished, state=row[0], seq=row[1], requester_id=str(row[2]),
            provider=row[3].removeprefix("mock-"), correlation_sha256=correlation_hash(row[4]),
            operation_ids=operation_ids,
            principals=[dict(principal_id=str(i), kind=kind, tenant_id=tenant, roles=roles)
                        for i, kind, tenant, roles in principals],
            agent_keys=[dict(key_id=str(i), principal_id=str(p), scopes=scopes, revoked=revoked)
                        for i, p, scopes, revoked in keys],
            proposals=[dict(proposal_id=str(p), proposed_by=str(actor), action=action,
                bound_seq=seq, bound_state=state, binding_digest=digest)
                for p, actor, action, seq, state, digest in proposals],
            decisions=[dict(proposal_id=str(p), decided_by=str(actor), decision=decision,
                decision_key=str(key)) for p, actor, decision, key in decisions],
            evidence=[dict(outcome_id=str(i), outcome=outcome, evidence_kind=kind, provenance=provenance)
                      for i, outcome, kind, provenance in evidence],
            audit=[dict(audit_id=i, event_type=kind, actor_id=str(actor), commit_xid=xid, details=details)
                   for i, kind, actor, xid, details in audit])
        self._database_samples.append(sample)
        return sample

    def _wait_state(self, local_id, state, *, timeout=18, predicate=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sample = self._snapshot(local_id, "wait_state")
            if sample["state"] == state and (predicate is None or predicate(sample)):
                return sample
            time.sleep(.1)
        raise RuntimeError(f"Qualification boundary did not observe committed {state}")

    def _stop_reconciler(self, emit):
        process = self.stack.processes["reconciler"]
        self.stack.kill("reconciler", signal.SIGTERM)
        self.stack.processes[f"prior-reconciler-{process.pid}"] = process
        emit("boundary_reconciler_stopped", pid=process.pid, returncode=process.returncode)

    def _reveal(self, emit):
        mock = self.slot.mocks["crm"]
        mock.fail_after_effect = False
        mock.index_lag = False
        mock.reindex()
        emit("boundary_provider_revealed", provider="crm")

    def drive_boundary(self, scenario, local_ids, emit):
        if scenario.family not in ("F14", "F15"):
            return
        require(len(local_ids) == 1, "Boundary admissions did not collapse to one operation")
        if scenario.family == "F14":
            self._drive_review(scenario, local_ids[0], emit)
        else:
            self._drive_agent(scenario, local_ids[0], emit)

    def _display_proposal(self, local_id, action):
        status, proposal = self._human("investigator", "POST", f"/actions/{local_id}/proposals",
            dict(action=action, rationale="Qualification: inspect actual provider evidence"), label="proposal_created")
        require(status == 201, "Qualification review proposal was refused")
        displayed = []
        for reviewer in ("reviewer", "reviewer2"):
            status, view = self._human(reviewer, "GET", f"/proposals/{proposal['proposal_id']}",
                                        label="proposal_displayed")
            require(status == 200 and view["binding_digest"] == proposal["binding_digest"],
                    "Reviewers did not display the same proposal binding")
            displayed.append(view)
        self._snapshot(local_id, "displayed_binding")
        return proposal

    def _decide(self, who, proposal, key, label="decision_response"):
        status, body = self._human(who, "POST", f"/proposals/{proposal['proposal_id']}/decision",
            dict(decision="approve", binding_digest=proposal["binding_digest"], decision_key=key), label=label)
        return who, key, status, body

    def _race_reviewers(self, local_id, proposal):
        s = sql.Identifier(self.env.schema)
        # No lifecycle mutation: hold the same row the protected function locks.
        with psycopg.connect(**local_admin_settings()) as blocker:
            blocker.execute(sql.SQL("SELECT operation_id FROM {}.safeact_operations "
                                     "WHERE operation_id = %s FOR UPDATE").format(s), (local_id,))
            blocker_pid = blocker.info.backend_pid
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self._decide, who, proposal, str(uuid.uuid4()))
                           for who in ("reviewer", "reviewer2")]
                try:
                    deadline = time.monotonic() + 3
                    while True:
                        started = time.monotonic_ns()
                        waiters = self.admin.execute(
                            "SELECT pid, pg_blocking_pids(pid) FROM pg_stat_activity "
                            "WHERE usename = %s AND wait_event_type = 'Lock'",
                            (self.env.api,)).fetchall()
                        # PostgreSQL may queue the second tuple-lock waiter behind
                        # the first waiter rather than directly behind our holder.
                        blocked = {blocker_pid}
                        for _ in range(len(waiters)):
                            blocked.update(pid for pid, parents in waiters if blocked.intersection(parents))
                        pids = sorted(blocked - {blocker_pid})
                        if len(pids) == 2:
                            self._boundary_event("reviewers_blocked", operation_id=local_id,
                                proposal_id=proposal["proposal_id"], source="database",
                                sample_started_ns=started, sample_finished_ns=time.monotonic_ns(),
                                api_backend_pids=pids, blocker_pid=blocker_pid,
                                blocking_edges=[dict(pid=pid, blocked_by=parents) for pid, parents in waiters])
                            break
                        require(time.monotonic() < deadline, "Two actual API decisions did not overlap on the row lock")
                        if any(f.done() for f in futures):
                            for future in futures:
                                if future.done():
                                    future.result()
                            raise RuntimeError("A review completed before overlap was observed")
                        time.sleep(.01)
                finally:
                    blocker.rollback()  # Always release before joining callers.
                return [future.result() for future in futures]

    def _drive_review(self, scenario, local_id, emit):
        require(scenario.review_mode in ("concurrent", "stale", "protected_retry"), "Unknown review scenario mode")
        retry = scenario.review_mode == "protected_retry"
        self._wait_state(local_id, "VERIFYING" if retry else "ESCALATED_UNRESOLVED",
            predicate=(lambda s: any(e["outcome"] == "proven_not_applied" for e in s["evidence"])) if retry else None)
        self._stop_reconciler(emit)
        proposal = self._display_proposal(local_id, "PROTECTED_RETRY" if retry else "REVERIFY")
        if scenario.review_mode == "stale":
            self._reveal(emit)
            self.stack.spawn("reconciler")
            # The real reconciler, not the harness, records the revealed effect.
            self._wait_state(local_id, "SUCCEEDED")
            result = self._decide("reviewer", proposal, str(uuid.uuid4()))
            require(result[2] == 409 and result[3].get("error", {}).get("code") == "stale_approval",
                    "Stale displayed review was not refused")
            sample = self._snapshot(local_id, "stale_refused")
            require(not sample["decisions"] and any(a["event_type"] == "APPROVAL_REFUSED_STALE"
                    for a in sample["audit"]), "Stale refusal was not independently committed")
        else:
            results = self._race_reviewers(local_id, proposal)
            winners = [r for r in results if r[2] == 200 and r[3].get("outcome") == "approved"]
            losers = [r for r in results if r[2] == 409 and r[3].get("error", {}).get("code") == "already_decided"]
            require(len(winners) == len(losers) == 1, "Review race did not produce one decision and one refusal")
            winner = winners[0]
            repeated = self._decide(winner[0], proposal, winner[1], "decision_replayed")
            require(repeated[2] == 200 and repeated[3].get("outcome") == "replayed", "Winning decision did not replay")
            sample = self._snapshot(local_id, "race_decided")
            require(len(sample["decisions"]) == 1, "Review race committed an incorrect decision count")
            self._reveal(emit)
            self.stack.spawn("reconciler")
            self._wait_state(local_id, "SUCCEEDED")
        self._snapshot(local_id, "review_complete")

    def _attack_snapshot(self, local_id, label):
        sample = self._snapshot(local_id, label)
        key = sample["correlation_sha256"]
        ledger = self.slot.ledger.events()
        dispatches = [e for e in ledger if e.get("event") == "provider_start"
                      and e.get("correlation_sha256") == key and e.get("method") == "create_record"]
        effects = [e for e in ledger if e.get("event") == "effect" and e.get("correlation_sha256") == key]
        sample["provider_dispatches"] = len(dispatches)
        sample["provider_effects"] = len(effects)
        return sample

    def _attack(self, local_id, label, action, expected):
        before = self._attack_snapshot(local_id, label + "_before")
        error, payload = action()
        require(error and payload.get("error") == expected, f"Agent attack {label} was not refused correctly")
        after = self._attack_snapshot(local_id, label + "_after")
        for field in ("operation_ids", "decisions", "evidence", "seq", "state", "provider_dispatches", "provider_effects"):
            require(before[field] == after[field], f"Agent attack {label} changed protected state: {field}")

    def _drive_agent(self, scenario, local_id, emit):
        self._wait_state(local_id, "ESCALATED_UNRESOLVED")
        self._stop_reconciler(emit)
        args = self._agent_arguments(scenario, scenario.operations[0])
        for key in self._agent_keys:
            error, same = self._agent(key, "submit_action", args, label="credential_replay")
            require(not error and same.get("operation_id") == local_id, "Rotating credentials changed operation identity")
        live = self._agent_keys[1]
        def tool(label, name, arguments, expected, key=live):
            self._attack(local_id, label, lambda: self._agent(key, name, arguments, label=label), expected)
        tool("changed_intent", "submit_action", {**args, "intent_ref": args["intent_ref"] + "-new"}, "conflicting_operation")
        tool("changed_payload", "submit_action", self._agent_arguments(scenario, scenario.operations[0], changed=True), "request_conflict")
        for field, value in (("tenant_id", "tenant-south"), ("actor_id", "another-actor"),
                             ("request_key", "caller-key"), ("provider_idempotency_key", "caller-provider-key")):
            tool("forbidden_" + field, "submit_action", {**args, field: value}, "invalid_arguments")
        operation = dict(operation_id=local_id)
        for name in ("get_action_status", "inspect_evidence", "request_verification", "propose_resolution"):
            extra = dict(action="REVERIFY", rationale="other tenant") if name == "propose_resolution" else {}
            tool("cross_tenant_" + name, name, {**operation, **extra}, "not_found", key=self._south_key)
        error, proposal = self._agent(live, "propose_resolution",
            {**operation, "action": "REVERIFY", "rationale": "Provider outcome requires human review"}, label="agent_proposal")
        require(not error and proposal.get("proposal_id"), "Agent could not propose a permitted resolution")
        self._snapshot(local_id, "agent_proposed")
        for name in ("approve_resolution", "decide", "safeact_api_decide", "approve"):
            tool("self_approval_" + name, name, dict(proposal_id=proposal["proposal_id"], decision="approve"), "unknown_tool")
        for credential_form in ("bearer", "cookie"):
            before = self._attack_snapshot(local_id, "human_endpoint_" + credential_form + "_before")
            started = time.monotonic_ns()
            headers = ({"Authorization": f"Bearer {live.key}"} if credential_form == "bearer" else
                       {"Cookie": f"{SESSION_COOKIE}={live.key}", CSRF_HEADER: "agent-cannot-mint-csrf"})
            with self._http() as http:
                response = http.post(f"/proposals/{proposal['proposal_id']}/decision", headers=headers,
                    json=dict(decision="approve", binding_digest="0" * 64, decision_key=str(uuid.uuid4())))
            self._boundary_event("self_approval_human_" + credential_form, key_id=str(live.key_id),
                proposal_id=proposal["proposal_id"], sample_started_ns=started,
                sample_finished_ns=time.monotonic_ns(), status_code=response.status_code)
            require(response.status_code == 401, "Human decision API accepted an agent credential")
            after = self._attack_snapshot(local_id, "human_endpoint_" + credential_form + "_after")
            require(all(before[k] == after[k] for k in ("operation_ids", "decisions", "evidence", "seq", "state", "provider_dispatches", "provider_effects")),
                    "Agent call to human endpoint changed protected state")
        with self.admin.transaction():
            revoke_agent_key(self.admin, self.env.schema, self._agent_keys[0].key_id)
        tool("revoked_credential", "submit_action", args, "unauthenticated", key=self._agent_keys[0])
        require(not self._snapshot(local_id, "agent_attacks_complete")["decisions"], "Agent committed a human decision")
        self._reveal(emit)
        self.stack.spawn("reconciler")
        self._wait_state(local_id, "SUCCEEDED")
        self._snapshot(local_id, "agent_complete")
