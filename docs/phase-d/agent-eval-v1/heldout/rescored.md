# heldout: scored under both rules

Raw results: `results.jsonl` (unedited). Rules: `agent-eval-scoring-v1` (strict: only each scenario's listed reports) and `agent-eval-scoring-v2` (in every scenario whose expected outcome is escalation, unresolved or needs_human_review is correct, for all arms; never success).

| Arm | Trials | Correct (v1 strict) | Correct (v2) | Justified esc. | Unjustified esc. |
|---|---|---|---|---|---|
| guarded_agent | 36 | 32 | 35 | 6/9 | 0 |
| direct_agent | 36 | 18 | 18 | 0/9 | 0 |
| deterministic | 36 | 36 | 36 | 9/9 | 0 |

## Trials whose verdict differs (3)

- `AG1-H07` guarded_agent #0: report=needs_human_review, v1=False, v2=True
- `AG1-H07` guarded_agent #1: report=needs_human_review, v1=False, v2=True
- `AG1-H07` guarded_agent #2: report=needs_human_review, v1=False, v2=True
