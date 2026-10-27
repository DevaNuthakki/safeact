# Pre-registration: held-out extension run

**Status:** registered on 2026-10-20 and committed **before any extension result exists**. The paid
command runs only after the author's go.

## What runs

**Trials:** 3 more trials per held-out scenario per arm, bringing the total to **6 trials per
scenario per arm**.

**Distinct trials:** indices 4–6 (one-based), which are 0-based indices 3–5, via `--first-trial 3`.
- The original run used 0-based indices 0–2.
- Each trial's targets, intent references and keys are seeded from the trial index, so the extension
  trials are new trials, not repeats. A Phase A test checks the six targets per scenario and arm are
  distinct.

**Nothing else changes.** It uses the same frozen held-out manifest,
`deploy/evaluation/agent_v1/manifest_heldout.json`, last changed in commit `3b023cd`:

- manifest_sha256: `a436243b5aedd1a0c8cf9ee45bba532c9ad646efe55e737aeaa84a7e80267564`

That means the same:
- model snapshot, `gpt-4.1-mini-2025-04-14`;
- prompt, `agent-prompt-v1`;
- tools, `safeact-guarded-v2` and `direct-mock-v1`;
- scoring, `agent-eval-scoring-v2`, also reported under v1 strict;
- scenarios, `agent-eval-v1` held-out;
- per-trial caps: 12 turns, 20 tool calls, 800 output tokens, $0.04;
- arms: guarded model, direct model, deterministic caller.

The run refuses to start if the manifest differs in any field (a Phase A test confirms
`--first-trial` leaves it unchanged).

**Command:**

```
SAFEACT_ALLOW_PAID_MODEL=1 .venv/bin/python -m deploy.evaluation.agent_v1.run --split heldout \
  --trials 3 --first-trial 3 --model openai --snapshot gpt-4.1-mini-2025-04-14 \
  --price-input 0.40 --price-cached 0.10 --price-output 1.60 --trial-usd 0.04 \
  --max-output-tokens 800 --manifest deploy/evaluation/agent_v1/manifest_heldout.json \
  --out docs/phase-d/agent-eval-v1/heldout-extension > .../console.log 2>&1
```

## Budget

| | |
|---|---|
| Paid trials | 12 scenarios × 3 trials × 2 model arms = 72 |
| Worst case | 72 × $0.04 = **$2.88** |
| Spent so far (all paid runs) | $0.2383 |
| Left of the $4.00 cap | **$3.7617**, enough for the worst case |
| Expected (at the original held-out rate) | about $0.15 |

## How results will be reported

There are three views, each under both scoring rules (v2, and v1 strict, via `rescored.md`):
1. **The original run:** `heldout/`, trials 1–3, unchanged.
2. **The extension:** `heldout-extension/`, trials 4–6.
3. **Combined:** `heldout-combined/`, trials 1–6. It is produced by
   `deploy/evaluation/agent_v1/combine.py`, which refuses runs made under different frozen manifests
   and refuses any trial counted twice.

**Every failure is listed** in each view. With 6 trials per scenario the numbers remain observed
counts, not rates.

**Pre-commitment:** the extension's results will be published whatever they show. Nothing in the
manifest, the harness or the scoring changes between the original run and the extension.
