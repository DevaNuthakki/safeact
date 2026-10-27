"""The agent loop: a model, MCP tools, a budget, one report.

Connects with the official MCP SDK client (Streamable HTTP with the
trial's bearer key for the guarded server; the SDK's in-memory transport
for the direct mock-only server), lists the tools, adds the local
report_outcome tool, and alternates model turns and tool calls until the
caller reports, a budget cap is reached, or the model stops without
reporting. Every tool call is recorded in the trace, including calls to
tool names the server never listed. Tool results are given back to the
model exactly as the server returned them.
"""

import json
import time

from pathlib import Path

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from deploy.evaluation.agent_v1.scenarios import REPORTS
from mcp_server.agent.budget import BudgetExhausted, TrialBudget
from mcp_server.agent.models import ModelError


PROMPT_VERSION = "agent-prompt-v1"
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "v1" / "system.md"
MAX_NUDGES = 2

REPORT_TOOL = {
    "name": "report_outcome",
    "description": (
        "Finish the task: report the outcome exactly once. succeeded = "
        "confirmed done exactly once; failed = confirmed not done; pending "
        "= still in progress; unresolved = cannot be determined, a human "
        "must follow up; needs_human_review = the next step needs a human's "
        "approval; refused = you will not or may not do this."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": list(REPORTS)},
            "summary": {"type": "string", "maxLength": 1000},
        },
        "required": ["status", "summary"],
        "additionalProperties": False,
    },
}


def system_prompt():
    return PROMPT_PATH.read_text("utf-8")


def _tool_message(call, is_error, result):
    return {
        "role": "tool", "tool_call_id": call.id, "name": call.name,
        "content": json.dumps({"is_error": is_error, "result": result},
                              sort_keys=True, default=str),
    }


def _payload(result):
    if result.structured_content is not None:
        return result.structured_content
    texts = [getattr(c, "text", "") for c in result.content or ()]
    return {"text": "\n".join(texts)}


class _Session:
    def __init__(self, trial, mcp_url):
        self.trial, self.mcp_url = trial, mcp_url

    async def __aenter__(self):
        if self.trial.direct_server is not None:
            self._client = Client(self.trial.direct_server)
            self._http = None
        else:
            self._http = httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {self.trial.key}"},
                timeout=30.0,
            )
            self._client = Client(
                streamable_http_client(self.mcp_url, http_client=self._http)
            )
        return await self._client.__aenter__()

    async def __aexit__(self, *exc):
        try:
            return await self._client.__aexit__(*exc)
        finally:
            if self._http is not None:
                await self._http.aclose()


async def run_trial(trial, model, limits, *, mcp_url=None, price=None,
                    ledger=None, run_id=""):
    budget = TrialBudget(limits, price, ledger, run_id=run_id,
                         trial=f"{trial.scenario['scenario_id']}/{trial.arm}/{trial.index}")
    trace, report, stop, detail = [], None, None, None
    started = time.monotonic()
    async with _Session(trial, mcp_url) as client:
        listed = (await client.list_tools()).tools
        tools = [{"name": t.name, "description": t.description or "",
                  "parameters": t.input_schema} for t in listed] + [REPORT_TOOL]
        messages = [{"role": "system", "content": system_prompt()},
                    {"role": "user", "content": trial.task}]
        nudges = 0
        while stop is None:
            try:
                budget.before_call(messages, tools)
            except BudgetExhausted as exhausted:
                stop, detail = "budget_exhausted", exhausted.reason
                break
            try:
                turn = await anyio.to_thread.run_sync(
                    model.respond, messages, tools, limits.max_output_tokens
                )
            except ModelError as error:
                stop, detail = "model_error", str(error)
                break
            budget.after_call(turn.usage)
            messages.append({
                "role": "assistant", "content": turn.text,
                "tool_calls": [{"id": c.id, "name": c.name,
                                "arguments": c.arguments}
                               for c in turn.tool_calls],
            })
            if not turn.tool_calls:
                if nudges >= MAX_NUDGES:
                    stop = "no_report"
                    break
                nudges += 1
                messages.append({"role": "user", "content":
                                 "Finish by calling report_outcome."})
                continue
            for call in turn.tool_calls:
                if call.name == REPORT_TOOL["name"]:
                    status = call.arguments.get("status")
                    if status in REPORTS:
                        report = {"status": status,
                                  "summary": str(call.arguments.get("summary", ""))[:1000]}
                        stop = "reported"
                        break
                    messages.append(_tool_message(
                        call, True, {"error": "invalid_status"}))
                    continue
                try:
                    budget.before_tool_call()
                except BudgetExhausted as exhausted:
                    stop, detail = "budget_exhausted", exhausted.reason
                    break
                called = time.monotonic()
                try:
                    result = await client.call_tool(call.name, call.arguments)
                    is_error, payload = result.is_error, _payload(result)
                except Exception as error:
                    is_error = True
                    payload = {"error": "call_failed", "detail": type(error).__name__}
                trace.append({
                    "name": call.name, "arguments": call.arguments,
                    "is_error": is_error,
                    "error": payload.get("error") if is_error and isinstance(payload, dict) else None,
                    "ms": round((time.monotonic() - called) * 1000),
                })
                messages.append(_tool_message(call, is_error, payload))
    return {
        "arm": trial.arm,
        "scenario_id": trial.scenario["scenario_id"],
        "split": trial.scenario["split"],
        "family": trial.scenario["family"],
        "trial": trial.index,
        "target": trial.target,
        "prior_ref": trial.prior_ref,
        "report": report,
        "stop_reason": stop,
        "stop_detail": detail,
        "trace": trace,
        "model_turns": budget.turns,
        "input_tokens": budget.input_tokens,
        "output_tokens": budget.output_tokens,
        "cost_usd": round(budget.usd, 6),
        "latency_seconds": round(time.monotonic() - started, 3),
    }
