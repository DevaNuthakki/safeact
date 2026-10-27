"""Hard budget caps: per trial and across every paid run.

Per trial: model turns, tool calls, output tokens per response, and a
USD cap. Across runs: one cumulative spend ledger (JSONL, no secrets)
with a total USD cap; every paid run reads it and appends to it as it
spends, so resumed and repeated runs all count.

Before every model call the worst case of that call is checked: the
input is estimated conservatively (one token per three characters) and
the output at the full max_output_tokens. If the worst case would cross
the trial cap or the total cap, the call is not made and the trial ends
as BUDGET_EXHAUSTED. After the call its actual cost (from the reported
usage) is recorded immediately.
"""

import json
import threading

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


CHARS_PER_TOKEN_ESTIMATE = 3


class BudgetExhausted(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Price:
    """USD per one million tokens."""

    input: float
    cached_input: float
    output: float

    def cost(self, input_tokens, output_tokens, cached_tokens=0):
        cached = min(cached_tokens, input_tokens)
        return (
            (input_tokens - cached) * self.input
            + cached * self.cached_input
            + output_tokens * self.output
        ) / 1_000_000


@dataclass(frozen=True)
class TrialLimits:
    max_turns: int = 12
    max_tool_calls: int = 20
    max_output_tokens: int = 800
    trial_usd: float = 0.10


def estimate_tokens(*parts):
    text = json.dumps(parts, default=str)
    return len(text) // CHARS_PER_TOKEN_ESTIMATE + 1


class SpendLedger:
    """Cumulative spend across all paid runs (append-only JSONL)."""

    def __init__(self, path, total_cap_usd):
        self.path = Path(path)
        self.total_cap = total_cap_usd
        self._lock = threading.Lock()

    def spent(self):
        if not self.path.exists():
            return 0.0
        total = 0.0
        for line in self.path.read_text("utf-8").splitlines():
            if line.strip():
                total += float(json.loads(line)["usd"])
        return total

    def remaining(self):
        return self.total_cap - self.spent()

    def record(self, *, run_id, trial, usd, input_tokens, output_tokens):
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id, "trial": trial, "usd": round(usd, 6),
            "input_tokens": input_tokens, "output_tokens": output_tokens,
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")


class TrialBudget:
    """One trial's caps, checked before each model call."""

    def __init__(self, limits, price=None, ledger=None, *, run_id="", trial=""):
        self.limits = limits
        self.price = price
        self.ledger = ledger
        self.run_id, self.trial = run_id, trial
        self.turns = 0
        self.tool_calls = 0
        self.usd = 0.0
        self.input_tokens = 0
        self.output_tokens = 0

    def worst_case(self, messages, tools):
        if self.price is None:
            return 0.0
        return self.price.cost(estimate_tokens(messages, tools),
                               self.limits.max_output_tokens)

    def before_call(self, messages, tools):
        if self.turns >= self.limits.max_turns:
            raise BudgetExhausted("max_turns")
        worst = self.worst_case(messages, tools)
        if self.usd + worst > self.limits.trial_usd:
            raise BudgetExhausted("trial_usd")
        if self.ledger is not None and worst > self.ledger.remaining():
            raise BudgetExhausted("total_usd")

    def after_call(self, usage):
        self.turns += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        if self.price is None:
            return
        usd = self.price.cost(usage.input_tokens, usage.output_tokens,
                              usage.cached_tokens)
        self.usd += usd
        if self.ledger is not None:
            self.ledger.record(
                run_id=self.run_id, trial=self.trial, usd=usd,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )

    def before_tool_call(self):
        if self.tool_calls >= self.limits.max_tool_calls:
            raise BudgetExhausted("max_tool_calls")
        self.tool_calls += 1
