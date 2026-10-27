# STOP 2 — PRE-RUN REVIEW

The author requested no commits and a pause before the final big run. This
packet records implementation and development verification only. Frozen smoke,
calibration and full qualification have **not** executed. No real Stripe or paid
model call is part of this work. Implementation and the complete local regression
are finished. Execution is paused at the author's requested review checkpoint.

## Comparison adapters

| Arm | Implemented behavior | Applicable dev cases | N/A dev cases |
|---|---|---:|---:|
| B0 | Intentionally incomplete synchronous caller; owned killable processes, memory-only acceptance, fresh payment keys, bounded retries | 35 | 40 |
| B1 | Durable jobs/outbox and late-ack queue retries; fresh payment keys, no provider lookup | 61 | 14 |
| B2 | B1 plus a stable native payment key; CRM/email retain their actual lack of native idempotency | 61 | 14 |
| B3 | Independent ordinary SQLAlchemy intent/outbox, fencing, native keys, conservative lookup/recovery, manual review | 72 | 3 |
| SafeAct | Protected lifecycle/evidence, authenticated human review and guarded agent boundary | 75 | 0 |

Each adapter uses the same connector contracts, budgets, pacing, mock providers
and fixed charge/order/refund semantics. B0 process observations are explicitly
memory-only; they cannot masquerade as committed database evidence. Native retry
identities retain every actual attempt alias. A separate inverse binds the real
charge reference and confirmed order failure. Duplicate inverses remain separate
from forward duplicates and orphan effects.

The selected development rows contain 12 B0, 16 B1 and seven B2 safety-failure
rows, with zero for B3 and SafeAct. These are development observations across
revisions and reruns, not a frozen comparison or a ranking. B2 legitimately
matches native-key payment cases; both robust implementations match on observed
safety. Earlier originals remain available, including scheduling-dependent
differences in useful completion. No performance advantage is claimed.

## What the evidence establishes

The complete development matrix has 375 selected rows. The archive retains 670
rows across eight development runs, including the two failed lifecycle cases.
All selected applicable rows have complete quiescence and no harness/integrity
error. Union coverage uses only valid retained development rows. SafeAct covers
all 28 declared labels; B3 covers all 25 applicable labels. Both prove native
claim/fence/outcome acknowledgement loss and an actual concurrent task refusal
while the original fenced attempt remains active.

Weaker-arm unobserved labels remain explicit in the replay summary. A missing
collapse/refusal guard is a baseline result. Conservative attribution also leaves
some after-send/unresolved labels uncredited when weak retries share a correlation
or no escalation occurs. Those cells are not silently converted to N/A or passes;
the actual calls, deaths, pending states and duplicate effects remain in raw data.

Independent breaker queries establish an open provider-account breaker with
unresolved committed work after actual failed HTTP. They do not claim that a
particular reconciler function ran. B3's F14 evidence retains its native binding,
policy, audit, lock waits and consumed one-use authority; no SafeAct audit or state
is fabricated. F15 is genuinely absent from baseline interfaces.

## Corrections before the freeze

- F13+F12 now targets the inverse lookup after a successful charge and confirmed
  order rejection. Provider/switch compatibility is validated before mutation.
- F06+F07 uses one held request, an eventual lookup contract and actual empty
  lookup ordering. F05+F08 has one worker-kill watcher.
- F16 achievable-workload declarations obey global rejection capacity and the
  eight-second investigation window. Twelve-second outages cannot borrow time
  from beyond it. CRM 429s retain their ambiguous contract classification.
- Pending workflows cannot invent escalation. A permanently rejected inverse
  remains pending unless a child has actual committed escalation evidence.
- Complete parked markers publish atomically. Owned shutdown requests are
  visible before signaling; failed signal/exit proof remains an integrity error.
- The generic read-only unacked decoder retains its old transport-test API.
  Privileged fault drivers still require reserved queues, DBs and ownership.
- A flaky API error test used a three-character hex secret that could occur in a
  random request UUID. Its distinctive synthetic replacement preserves the
  no-leak assertion without changing API behavior.

The original diagnostics and corrected checks are retained. Earlier sealed
packets remain immutable. Held-out definitions were audited from source and
contracts; no held-out outcomes were executed or used for tuning.

## Frozen inputs and budget

[manifest_qual_v1.json](../../../chaos/manifest_qual_v1.json) pins 1,200 cases:
60 no-fault controls, 1,020 F01–F16 cases and 120 held-out combinations. The
disjoint qualification seed is 510112026. The smoke sample has 80 fixed cases.
Contract/timing, applicability, coverage, scoring and 230 backend/deployment
source files are hashed and checked before service access.

The primary budget is 6,000 scenario/arm rows, one primary invocation, at most
24 hours and seven reserved slots. No external provider or model spend is
authorized. A durable invocation claim prevents quietly replacing originals.
Budget stops retain unscheduled denominators; SafeAct safety failures stop new
scheduling and block qualification. Diagnostic reruns preserve the original
failure and stay outside headline counts.

Slots remain unset at STOP 2. After approval, run the same frozen smoke sample at
1, 2, 4, 6 and 7 slots. Retain every candidate and choose by sampled load only.
A verified amendment is required before full outcomes. Full execution also
requires the calibrated machine/OS; the accepted laptop run requires AC power
and active caffeinate. The manual CI workflow is prepared and YAML-parsed, but
has not been dispatched or claimed as passing.

The work remains uncommitted. File digests pin this working tree; no commit or
push was made. The final big run, load test, Compose packaging and final demo/
evidence pack remain after this checkpoint.

## Reproduce the read-only review

```sh
PYTHONPATH=. .venv/bin/python docs/phase-e/dev-baseline-adapter-20261023/tools/verify_packet.py
PYTHONPATH=. .venv/bin/python docs/phase-e/dev-baseline-adapter-20261023/replay.py docs/phase-e/dev-baseline-adapter-20261023
PYTHONPATH=. .venv/bin/python docs/phase-e/dev-baseline-adapter-20261023/counterexamples.py docs/phase-e/dev-baseline-adapter-20261023
```

These commands read saved bytes only. [regression-checks.json](regression-checks.json)
records final results and links the retained logs; [test-inventory.json](test-inventory.json)
lists all 1,626 unique Python test IDs. Final checks passed:

| Check | Passing tests |
|---|---:|
| Phase A | 901 |
| PostgreSQL, broker and adapter integration | 693, no skips |
| Isolated security | 21 |
| API end to end | 9 |
| Scripted agent | 2 |
| Console unit | 37 |
| Browser end to end | 14 |

Type checking, linting and the production build also pass. Full integration took
1,288.132 seconds. Only two CI workflow files changed after that run began;
[the source proof](integration-source-proof.json) reconstructs its exact starting
manifest and establishes that Python SUT bytes were unchanged. Phase A passed
again against the amended final pin. Both workflow files parse; hosted CI has
not been executed.

[source-sha256.json](source-sha256.json) inventories 470 archived source files and
four fixture files retained as exact base-commit references. The four references
are excluded from the shared archive because their synthetic secret-shaped
fixtures trigger the scanner. All other source, saved evidence and shared logs
pass the scanner. Shared logs redact standalone 64-hex values; original log
digests and redaction counts are retained. Original diagnostic failures remain
available alongside corrected passing runs.

[artifact-sha256.json](artifact-sha256.json) seals the packet.
[historical-packets.json](historical-packets.json) records unchanged hashes for
364 artifacts in nine earlier sealed packets. The development archives retain
29 original run files and 25 focused captures. Read-only replay passes, rejects
25 new saved-data mutations and preserves the earlier 33-mutation check.

## After the author's explicit go

The next execution will be announced prominently as **FINAL BIG QUALIFICATION
RUN** before it starts. First execute the pinned 80-case sample across all five
arms at 1, 2, 4, 6 and 7 slots, retaining every candidate. Verify a separate
calibration amendment and choose concurrency using sampled machine load only.
This amendment keeps the reviewed input manifest immutable and requires no
commit. Then execute the single primary 1,200-case, five-arm qualification on
the calibrated machine within the pinned budget. Any diagnostic reruns remain
separate from original results. Load testing, Compose/demo packaging and the
final presentation follow qualification. None of these post-review executions
is authorized by the continuation that produced this packet.
