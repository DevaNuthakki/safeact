"""Agent evaluation v1 (Milestone 4, blueprint 06 and 09).

The same tool-calling model with guarded SafeAct tools versus direct
provider tools, plus a deterministic caller on the same safety layer,
on versioned development and held-out scenarios against the mock
providers only. Ground truth comes from the harness effects ledger and
the database, never from what a caller reports.
"""
