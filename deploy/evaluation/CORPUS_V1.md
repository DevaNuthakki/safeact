# SafeAct Evaluation Corpus v1 — Design Contract

## Purpose

Evaluate correctness of SafeAct's admission, recovery, and
failure-handling decisions. This corpus is not machine-learning
training data and contains no real provider credentials or records.

The dataset is a **design-stage inventory**, not a completed
evaluation. Merely adding a scenario does not count as passing it.

## Required evidence distinctions

- `reference_reviewed`: relevant existing test assertions were
  inspected. It does not mean this corpus scenario was run.
- `partial_reference`: related evidence exists, but an important
  part of the proposed scenario remains unverified.
- `not_established`: the claimed behavior lacks demonstrated
  coverage in the reviewed evidence.

The `source_kind` states what type of evidence exists or is
planned. A simulated provider is not a real provider; a policy
unit test is not an end-to-end distributed test.

## Corpus lifecycle

All initial cases use `partition=design`,
`execution_status=NOT_RUN`, and
`oracle_implementation=UNIMPLEMENTED`.

The independent oracle, scenario runner, controlled fault
injection, and measurable results are future work.

Do not describe this corpus as a benchmark result until those
pieces are implemented and executed.

## Scenario record fields

Each JSON Lines record contains:

- A stable `scenario_id`, title, category, and priority
- Evidence provenance and current limitations
- Explicit preconditions and ordered failure events
- Machine-readable expected assertions
- Existing reference test methods, where relevant
- Execution and oracle implementation status

The validator checks data structure, stable identities,
evidence terminology, and source references. It does not
execute recovery behavior or prove an expected outcome.

## Oracle observations

- `policy_decision`: actual recovery policy output
- `provider_oracle`: simulated or provider-observed effects
- `scheduler_trace`: independently captured worker/dispatch events
- `database_snapshot`: durable database rows after a test
- `validator_output`: validation result
- `sql_gate`: authoritative database admission result

A future runner must implement each observer independently.
It must not accept SafeAct's own reported success as the
sole evidence of actual external effects.

For `bundle_counts`, the ordering is:
`[operations, attempts, audit_events, outbox_events]`.

## Dataset quality rules

1. A scenario must cover a meaningful failure or trust boundary.
2. Expected decisions must be set before an evaluation run.
3. Tests must distinguish policy checks, simulations, SQL
   transactions, and true provider effects.
4. A failed or inconclusive scenario is recorded, not deleted.
5. Evaluation case IDs and expected oracles are versioned.
6. The final holdout set is not chosen or frozen yet.
7. Once frozen, holdouts cannot be used to tune the implementation.
8. Reproducible fault scheduling, seeds, and environmental
   dependencies must be captured before promoting cases.
9. Safety failures must be reported separately from liveness
   failures; a justified HOLD is not a false failure.
10. No real irreversible provider dispatch is authorized
    by this evaluation specification.

## Known implementation risks to track

The legacy SQL gate has documented limitations concerning
canonical JSON enforcement and runtime subject assertion.
The disposable HMAC gate mitigates the tested proof-forgery
path but is not a complete production trust boundary.

Host PostgreSQL loopback authentication has `trust` rules.
Local TCP tests are not evidence of SCRAM enforcement.
The isolated Docker PostgreSQL experiment independently
demonstrated SCRAM authentication.

Other pending gates include signer workload authentication,
TLS or equivalent protected transport, secret custody,
key rotation, commit-acknowledgment uncertainty, real
multi-worker concurrency, and production recovery policy.

## Promotion requirements

Promote each design case only after:

1. Its system-under-test contract and expected decision match
   the actual implementation.
2. An independent and executable oracle exists.
3. Fault scheduling is deterministic or its variability measured.
4. Both normal and adversarial tests are repeatable.
5. Logs record actual outcomes and the environment.
6. No contract thresholds were loosened after observation.

Only after these gates should we select and freeze an
independent final holdout evaluation set.
