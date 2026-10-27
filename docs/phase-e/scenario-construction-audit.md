# Construction and scoring audit before STOP 2

Review source and contracts only; do not execute held-out combinations. Preserve
the original development results when declarations change. These corrections
precede the candidate freeze and require fresh development checks where affected.

- F13+F12 must target the inverse payment lookup after the charge and confirmed
  order rejection. Use an explicit inverse-only lookup control, activated by
  the refund provider entry. Choose a lost or hidden inverse reply; never move
  CRM's `corrupt_search` onto payments or corrupt the initial charge lookup.
- F06+F07 needs one accepted request held past the lease and released after a
  real empty lookup. Use a six-second request contract with eventual payment
  lookup, or the existing eventual CRM contract. Hold its HTTP reply too; two
  independent one-shot holds do not establish the same-request combination.
- F05+F08 has one selected worker-kill boundary plus queued loss. Drop the
  optional second F08 worker kill, whose watcher would compete for the same
  parked-process marker. F16+F11 computes achievability from the final connector,
  deadline and acknowledgement boundary, not another randomly selected provider.
- Validate all provider/switch combinations before fault mutation or admission.
  Unknown switches and unsupported provider controls are harness errors.
- F16 global 429 counts cannot promise completion of every requested operation
  when rejections exceed n*(max_attempts-1). Use that contract capacity test.
  Outages crossing the eight-second investigation window cannot be called
  automatically achievable under that window. The twelve-second deadline cases
  are therefore unachievable for both providers. These cases stay in safety and
  all-requested outcome reports; only the separately declared achievable-workload
  usefulness denominator excludes them. Partial completions remain visible.
  CRM 429s remain ambiguous under its connector contract; private mock knowledge
  of non-application cannot authorize an automatic retry.
- Native retry identities must belong to the recorded arm and actual bounded
  attempt ordinals. Attribute every alias, including duplicate inverses, without
  treating those effects as unbound orphans. Credit an actual lost inverse reply
  even when another retry creates a prohibited duplicate; safety is scored
  separately. Process snapshots for B0 stay distinct from committed DB evidence.
  A permanently rejected inverse makes the business workflow unresolved; it
  cannot invent a lifecycle escalation. SafeAct and B3 report it as pending
  unless a child has an actual committed escalation state or timestamp.
- Coverage requirements follow absent mechanisms. An unachieved guard such as
  duplicate collapse is a baseline result, not an absent-feature N/A. Record
  all observed labels, per-arm applicable labels, and all missing labels openly.
- The first concurrent weaker development run exposed an empty parked-marker
  race (B2 F08/0006): a watcher opened the file between creation and JSON write.
  Publish the complete closed marker atomically without overwriting an existing
  marker. Keep malformed observations fatal; rerun the affected development case.
- The corrected run exposed a second lifecycle race (B1 F05/0001): the observer
  could poll the deliberately killed worker after SIGKILL but before `wait()`
  recorded its expected exit. Record the owned kill request before signaling,
  prove its exit before replacement, and retain failed requests as integrity
  errors. Never ignore an exit without an explicit owned shutdown request.

Candidate freeze must hash source, contracts, construction, scoring, applicability,
coverage, concrete scenario JSONL and execution budget. STOP 2 remains mandatory.

The final comparison also needs the accepted plan's boundary detail: retain
commit-ack proof separately for native claim/fence/outcome or delivery/outcome
stages, and distinguish actual concurrent delivery from a proved refusal while
the original fenced attempt remains active. Add independent read-only samples of
the provider-account breaker joined to unresolved committed work, after real HTTP
failure. This records existing pacing without changing any limiter, transaction,
deadline or retry choice. Do not call a pending-state sample proof that a specific
reconciler function executed. The observed label states precisely the DB fact.

The first final Phase A run exposed an unrelated flaky security fixture: it
rejected the substring `abc` anywhere in an error response, including a random
request UUID that happened to contain those three hex characters. The response
contained only the generic error and request ID. Use a distinctive non-hex
synthetic secret and retain the no-secret-in-response assertion. No API error
handling or redaction behavior changes; preserve the original failed log.

The first full integration run caught a read-only decoder compatibility
regression: the old generic SafeAct unacked decoder accepts an exact matching
transport-test queue, while privileged qualification faults require reserved
queues/DBs. Preserve that decoder API and retain strict ownership checks in
fault-driver constructors. The default still rejects baseline task envelopes.
Keep the original failure and rerun the actual Redis protocol check plus the
complete integration suite with the fix.

The new common-arm integration tests need Toxiproxy in regular CI as well as
the manual workflow. Linux CI runs the pinned proxy with host networking and a
loopback API/upstream, so it can reach loopback mock servers. The cached image's
entrypoint and `-host` option were inspected locally. Both workflow files parse;
no workflow was dispatched. This CI-only amendment changes no Python SUT code.
