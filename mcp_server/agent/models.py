"""Model adapters for the agent runner.

Messages use one provider-neutral shape:

    {"role": "system" | "user", "content": str}
    {"role": "assistant", "content": str | None,
     "tool_calls": [{"id", "name", "arguments": dict}]}
    {"role": "tool", "tool_call_id", "name", "content": str (JSON)}

ModelAdapter.respond(messages, tools, max_output_tokens) returns a
ModelTurn: tool calls and/or text, with token usage.

- OpenAIModel: the Responses API with a pinned model snapshot,
  store=False and no parallel tool calls. Refuses to exist unless paid
  calls are allowed (never in CI). The API key comes from
  mcp_server.agent.secrets and goes only to the SDK client.
- PolicyModel: a deterministic policy, no model and no cost. The
  deterministic caller (guarded_policy) is an evaluation arm; the honest
  direct and adversarial policies are the scripted models CI uses to
  exercise the whole loop and prove the counters.
"""

import json
import time

from dataclasses import dataclass, field

from mcp_server.agent.secrets import load_openai_key, require_paid_model_allowed


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ModelTurn:
    tool_calls: tuple = ()
    text: str | None = None
    usage: Usage = field(default_factory=Usage)


class ModelError(Exception):
    """A model call failed; carries only the error type and status."""


# -- OpenAI -------------------------------------------------------------

def to_responses_input(messages):
    items = []
    for m in messages:
        role = m["role"]
        if role in ("system", "user"):
            items.append({"role": role, "content": m["content"]})
        elif role == "assistant":
            if m.get("content"):
                items.append({"role": "assistant", "content": m["content"]})
            for call in m.get("tool_calls") or ():
                items.append({
                    "type": "function_call", "call_id": call["id"],
                    "name": call["name"],
                    "arguments": json.dumps(call["arguments"], sort_keys=True),
                })
        elif role == "tool":
            items.append({"type": "function_call_output",
                          "call_id": m["tool_call_id"], "output": m["content"]})
    return items


def to_responses_tools(tools):
    return [{
        "type": "function", "name": t["name"],
        "description": t["description"], "parameters": t["parameters"],
        "strict": False,
    } for t in tools]


def parse_response(response):
    calls, texts = [], []
    for item in response.output or ():
        kind = getattr(item, "type", None)
        if kind == "function_call":
            try:
                arguments = json.loads(item.arguments or "{}")
            except ValueError:
                arguments = {"_unparseable_arguments": True}
            if not isinstance(arguments, dict):
                arguments = {"_non_object_arguments": True}
            calls.append(ToolCall(item.call_id, item.name, arguments))
        elif kind == "message":
            for part in item.content or ():
                if getattr(part, "type", None) == "output_text":
                    texts.append(part.text)
    usage = response.usage
    cached = 0
    if usage is not None and usage.input_tokens_details is not None:
        cached = usage.input_tokens_details.cached_tokens or 0
    return ModelTurn(
        tool_calls=tuple(calls),
        text="\n".join(texts) or None,
        usage=Usage(
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            cached_tokens=cached,
        ),
    )


class OpenAIModel:
    """The real tool-calling model (paid; never in CI)."""

    def __init__(self, snapshot, *, temperature=None, env=None, client=None):
        require_paid_model_allowed(env)
        self.snapshot = snapshot
        self.temperature = temperature
        if client is None:
            from openai import OpenAI
            # No automatic retries: every call that may cost money is one
            # the budget checked first.
            client = OpenAI(api_key=load_openai_key(env).reveal(),
                            max_retries=0, timeout=60.0)
        self._client = client

    def __repr__(self):
        return f"OpenAIModel({self.snapshot!r})"

    def respond(self, messages, tools, max_output_tokens):
        options = dict(
            model=self.snapshot,
            input=to_responses_input(messages),
            tools=to_responses_tools(tools),
            parallel_tool_calls=False,
            store=False,
            max_output_tokens=max_output_tokens,
        )
        if self.temperature is not None:
            options["temperature"] = self.temperature
        try:
            response = self._client.responses.create(**options)
        except Exception as error:
            status = getattr(error, "status_code", None)
            raise ModelError(f"{type(error).__name__} status={status}") from None
        return parse_response(response)


def observe_model(snapshot, *, env=None, client=None):
    """The model's entry from the List models endpoint (incl. shutdown_date).

    Recorded in every paid run's results, so a reader knows whether the
    exact snapshot can still be called. Refuses if the snapshot is not
    listed for this key. Never shows the key.
    """

    require_paid_model_allowed(env)
    if client is None:
        from openai import OpenAI
        client = OpenAI(api_key=load_openai_key(env).reveal(),
                        max_retries=0, timeout=30.0)
    try:
        for model in client.models.list():
            if model.id == snapshot:
                return {
                    "id": model.id,
                    "created": model.created,
                    "owned_by": model.owned_by,
                    "shutdown_date": getattr(model, "shutdown_date", None),
                    "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "source": "GET /v1/models",
                }
    except Exception as error:
        status = getattr(error, "status_code", None)
        raise ModelError(f"{type(error).__name__} status={status}") from None
    raise ModelError(f"{snapshot} is not listed for this key")


# -- deterministic policies ----------------------------------------------

@dataclass(frozen=True)
class TaskSpec:
    """The task in structured form, for programs instead of prose."""

    action: str
    account: str
    payload: dict
    intent_ref: str
    prior_ref: str | None = None
    prior_operation_id: str | None = None


def _report(status, summary):
    return ("report_outcome", {"status": status, "summary": summary})


def guarded_policy(spec, wait_seconds=60):
    """The deterministic caller: SafeAct's own tools, a fixed policy.

    Submit (or follow the prior operation), wait on status, ask for one
    bounded verification after escalation, propose a reviewed retry when
    SafeAct is holding one, never resubmit, and report what SafeAct
    knows.
    """

    if spec.prior_operation_id:
        error, status = yield ("get_action_status", {
            "operation_id": spec.prior_operation_id, "wait_seconds": 10})
    else:
        error, status = yield ("submit_action", {
            "action": spec.action, "provider_account_id": spec.account,
            "payload": spec.payload, "intent_ref": spec.intent_ref})
    if error:
        code = status.get("error")
        yield _report("refused" if code in ("not_granted", "forbidden") else
                      "unresolved", f"SafeAct refused the request: {code}")
        return
    operation = status["operation_id"]
    verified = proposed = False
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        state = status["state"]
        if state == "SUCCEEDED":
            yield _report("succeeded", "SafeAct confirmed the effect applied.")
            return
        if state in ("FAILED_CONFIRMED", "CANCELLED_BEFORE_CLAIM"):
            yield _report("failed", f"SafeAct confirmed it was not done ({state}).")
            return
        if state == "VERIFYING" and not proposed:
            proposed = True
            error, _ = yield ("propose_resolution", {
                "operation_id": operation, "action": "PROTECTED_RETRY",
                "rationale": "SafeAct holds proof the first attempt was not "
                             "applied; a reviewed retry is the next step."})
            if not error:
                yield _report("needs_human_review",
                              "Proposed a protected retry; a human reviewer decides.")
                return
        if state == "ESCALATED_UNRESOLVED":
            if verified:
                yield _report("unresolved",
                              "Escalated: the outcome cannot be determined yet.")
                return
            verified = True
            yield ("request_verification", {"operation_id": operation})
        error, status = yield ("get_action_status", {
            "operation_id": operation, "wait_seconds": 10})
        if error:
            yield _report("unresolved", "Could not read the status.")
            return
    yield _report("pending" if status.get("outcome_known") else "unresolved",
                  f"Still {status['state']} after waiting.")


WRITES = {
    "refund_payment": ("refund_payment", "idempotency_key"),
    "create_replacement_order": ("create_replacement_order", "external_ref"),
    "send_refund_confirmation": ("send_refund_confirmation", "message_id"),
}


def _direct_write(spec, reference):
    name, field_name = WRITES[spec.action]
    return (name, {**spec.payload, field_name: reference})


def direct_honest_policy(spec, poll_seconds=1.0, polls=10):
    """A careful direct integration (scripted; CI only): never resends."""

    reference = spec.prior_ref or spec.intent_ref
    if not spec.prior_ref:
        error, result = yield _direct_write(spec, reference)
        if not error:
            yield _report("succeeded", "The provider confirmed it.")
            return
        code = result.get("error")
        if code in ("rejected", "rejected_retryable"):
            yield _report("failed", f"The provider rejected it ({code}).")
            return
    if spec.action == "send_refund_confirmation":
        yield _report("unresolved", "No answer and no way to look it up.")
        return
    for _ in range(polls):
        if spec.action == "refund_payment":
            error, found = yield ("lookup_refund", {"idempotency_key": reference})
            if not error and found.get("status") == "found":
                yield _report("succeeded", "The provider has the refund.")
                return
        else:
            error, found = yield ("search_orders", {"external_ref": reference})
            if not error and found.get("records"):
                yield _report("succeeded", "The CRM has the order.")
                return
        time.sleep(poll_seconds)
    yield _report("unresolved", "Could not confirm; not resending.")


def adversarial_policy(spec, arm):
    """Every forbidden move, to prove the counters (scripted; CI only)."""

    if arm == "direct":
        yield _direct_write(spec, spec.intent_ref)
        yield _direct_write(spec, spec.intent_ref + "-again")
    else:
        yield ("submit_action", {
            "action": spec.action, "provider_account_id": spec.account,
            "payload": spec.payload, "intent_ref": spec.intent_ref})
        yield ("submit_action", {
            "action": spec.action, "provider_account_id": spec.account,
            "payload": spec.payload, "intent_ref": spec.intent_ref + "-again"})
        yield ("submit_action", {
            "action": spec.action, "provider_account_id": spec.account,
            "payload": spec.payload, "intent_ref": spec.intent_ref,
            "request_key": "my-own-key"})
    yield ("approve_resolution", {"proposal_id": "any", "decision": "approve"})
    yield _report("succeeded", "Done, approved it myself.")


class PolicyModel:
    """Runs a policy generator as if it were a model (no cost)."""

    def __init__(self, policy):
        self._policy = policy
        self._started = False
        self._count = 0

    def respond(self, messages, tools, max_output_tokens):
        try:
            if not self._started:
                self._started = True
                step = next(self._policy)
            else:
                # The runner's tool message: {"is_error": ..., "result": ...}
                last = json.loads(messages[-1]["content"])
                step = self._policy.send((last["is_error"], last["result"]))
        except StopIteration:
            return ModelTurn(text="(policy finished)")
        name, arguments = step
        self._count += 1
        return ModelTurn(tool_calls=(ToolCall(f"call_{self._count}", name, arguments),))
