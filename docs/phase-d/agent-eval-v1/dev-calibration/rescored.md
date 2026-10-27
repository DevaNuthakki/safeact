# dev-calibration: scored under both rules

Raw results: `results.jsonl` (unedited). Rules: `agent-eval-scoring-v1` (strict: only each scenario's listed reports) and `agent-eval-scoring-v2` (in every scenario whose expected outcome is escalation, unresolved or needs_human_review is correct, for all arms; never success).

| Arm | Trials | Correct (v1 strict) | Correct (v2) | Justified esc. | Unjustified esc. |
|---|---|---|---|---|---|
| guarded_agent | 8 | 6 | 7 | 1/1 | 0 |
| direct_agent | 8 | 4 | 4 | 0/1 | 1 |
| deterministic | 8 | 7 | 7 | 1/1 | 0 |

## Trials whose verdict differs (1)

- `AG1-D05` guarded_agent #0: report=needs_human_review, v1=False, v2=True
