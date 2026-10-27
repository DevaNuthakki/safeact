# Phase A — Late-commit / empty-lookup experiment

## Status

Deterministic simulation of the unsafe B0 baseline.

This is not yet an implementation of SafeAct's
production recovery policy.

## Failure hypothesis

A remote request can be accepted while its response
is lost. A lookup performed before the remote commit
may return NOT_FOUND.

That lookup does not establish that the pending
request will never apply.

## Connector assumptions

- No provider-enforced idempotency.
- Dispatch accepts work before acknowledgement loss.
- Accepted requests may remain pending.
- Lookup cannot observe pending requests.
- NOT_FOUND is non-authoritative.
- No definitive rejection evidence is available.

## Controlled interleaving

1. Dispatch A for one logical refund operation.
2. Provider accepts A but caller receives a timeout.
3. Lookup returns NOT_FOUND while A remains pending.
4. Intentionally naive baseline dispatches B.
5. Provider commits A and B.
6. Independent evaluation ledger records two effects.

## Required observation

The naive baseline violates the at-most-one-effect
safety invariant for the simulated connector contract.

## SafeAct decision requirement

For the same connector capabilities and evidence,
SafeAct must not authorize an unsupported second
dispatch.

The operation must remain uncertain until reliable
evidence or an authorized resolution path exists.

A lookup returning NOT_FOUND must not be silently
promoted into definitive non-application.

## Evidence boundaries

The simulated provider exposes lookup information
separately from the independent effects ledger.

Only tests may inspect the ground-truth ledger.

These tests demonstrate one controlled failure
interleaving. They do not establish production
correctness or universal duplicate prevention.

## Reproduction

Run from the repository root:

python3 -m unittest discover -s tests -p 'test_*.py' -v

## Next implementation gate

Implement versioned connector capabilities and
a pure recovery decision policy.

Test that the policy rejects the unsupported retry
under exactly these simulated conditions.
