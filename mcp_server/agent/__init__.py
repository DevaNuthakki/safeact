"""The agent runner: one tool-calling loop over MCP tools (Milestone 4).

The same loop drives every arm of the evaluation: a real model with the
guarded SafeAct tools, the same model with direct provider tools, and
deterministic policies (the deterministic caller, and the scripted
models CI uses instead of a paid model). Budgets are enforced before
every model call; the OpenAI key is read only from a file outside the
repository and never logged.
"""
