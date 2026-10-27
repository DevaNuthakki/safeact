# Lookup completion must remain inside the native-key evidence window

2026-10-23. Found while implementing the independent B3 execution runtime.
This is a Python evidence-classification fix before freeze; no frozen SQL,
legal transition, authorization grant or migration changes.

## Reproduced counterexample

An actual HTTP payment request applies once and loses its reply. Its q-v2-short
key has six-second retention and a two-second margin. A lookup starts at about
three seconds, while its pre-request DB observation is inside that window. The
HTTP mock handles it only after actual provider key-cache expiry at six seconds,
then correctly returns absence. The connector normalized using the old target
time, and SafeAct persisted `proven_not_applied` despite an independent applied
effect. The original failing regression is retained, not discarded.

The lookup timing crosses a contract boundary. Starting before the boundary
cannot make an answer obtained afterward authoritative. This does not affect
positive, payload-matching effect receipts or a real pre-execution rejection.

## Fix and verification

Introduce a shared connector result-completion check for native-key negative
lookup evidence. The reconciler obtains fresh database time after the network
response and checks the original first-fence window again before persisting
proof. A crossed/unknown window downgrades the result to non-decisive NOT_FOUND;
an inconsistent clock downgrades to INCONCLUSIVE. Preserve provider facts.

B3 applies that same classification rule using its post-response database time.
Its independent evidence also limits a negative lookup to attempts already
present at reservation, so an old answer cannot certify a later dispatch.

Verify the real HTTP/DB counterexample without edited clocks, timestamps or
fabricated non-application references; retain one actual effect, one dispatch,
non-decisive evidence and uncertainty. Test boundary equality, missing/reversed
timestamps, facts/provenance preservation and unaffected positive/rejection
semantics, then run the complete regression suites. No real Stripe/model call
is needed. A final qualification run remains unauthorized.
