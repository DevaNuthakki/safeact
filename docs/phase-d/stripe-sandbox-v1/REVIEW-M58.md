# M5.8 Stripe sandbox evidence review

Run: [run-m58-1f1b0a7e2db0](run-m58-1f1b0a7e2db0/).
Started: 2026-10-21T21:16:54.482105+00:00; finished: 2026-10-21T21:19:37.894799+00:00.
Base commit: 980b55bfa4a20604d593bebbb76f2d9fde658cf9; 18 M5.9 working files were present.
The runner, adapter, key guard, fake and manifest matched HEAD at execution.
This evidence supports the declared sandbox experiment, not full qualification.

Offline and real sandbox runs: 16 PASS each; 0 DIFFERENT; 0 ERROR.
Real run: ST-01 through ST-08, two repeats; 77/150 HTTP calls.
API: 2026-09-30.endive; Python: 3.13.1.
Client: macOS-15.4.1-x86_64-i386-64bit-Mach-O. Reported sandbox cost: 0 (Stripe sandbox; no charges).

Both repeats observed:
- ST-01: confirmed refund; one POST and one provider refund.
- ST-02: lost response after application; lookup confirmed one refund;
  one refund POST.
- ST-03: same-key replay returned the same reference; one refund.
- ST-04: changed parameters gave an ambiguous key conflict; one refund.
- ST-05: concurrent same-key requests returned 200/409; one refund.
- ST-06: one POST, zero observed refunds, ESCALATED_UNRESOLVED after
  insufficient evidence; no unsupported retry. Safety pass is not completion.
- ST-07: first operation succeeded, second FAILED_CONFIRMED; one refund total.
- ST-08: listing observed after 1628/1317 ms, one lookup attempt each.
There were 14 distinct provider refund IDs. Listing timings are observations,
not consistency guarantees. Refund status is non-decisive provider evidence;
later asynchronous failures and webhooks are outside this experiment.

Source review confirmed restricted test keys, sandbox opt-in, CI refusal,
PaymentIntent livemode=false requirement, and refusal of live refund objects.
Saved projections omit livemode and do not independently audit every flag.
No credential contents were reviewed.

Raw manifest SHA256: 138973aaef8a6b91f551afdc417fa97e17682d441cd13d83d38a927e0f4f853a
Sorted JSON digest used by runner: fb7160a5f2314fe8a8912477c791380a3352811ceabba64dca452b2c5bdfa29f
These hashes use different serializations of the same manifest.
Runner SHA256 at execution: 3000f1cd89f06a9d5ba69aead4a04190a91f209fc3d061ed882e41e149598922

Artifact SHA256 values:
- [summary.json](run-m58-1f1b0a7e2db0/summary.json): 62740d9feb2d7611dbd9738c06d606526af4890b819e0d6774fb285c92a4495d
- [results.jsonl](run-m58-1f1b0a7e2db0/results.jsonl): 76e88dd73ccf18f5557f9b044c935760cf2fca67477408475c57947aeb63e970
- [calls.jsonl](run-m58-1f1b0a7e2db0/calls.jsonl): c1e65a59bc153cd584ad723330d04d22938b81c4cdcbb7aaeb3cc4f4cfa1b8b3

Follow-up patch: return nonzero for DIFFERENT or ERROR, close test file
handles, configure Alembic path_separator=os, and reflect native xid8 using
a registry copied for each disposable test engine. No warning filter hides
the xid8 warning. Three new regression tests cover failure artifacts/exit
status and native PostgreSQL type reflection. Post-change regressions reviewed on 2026-10-21: Python 1053
(492 Phase A, 530 integration, 21 isolated, 8 API E2E, 2 agent harness),
Vitest 25 and Playwright 10; typecheck, lint and build passed. No skipped
tests were reported. The three targeted warnings were absent.

Baseline regressions passed: Python 1050 and console 35; typecheck/lint/build.
Runtime error logs remain visible and their test attribution is unresolved.
SQL canonical-JSON enforcement and runtime credential/subject binding remain
stated limitations; this patch does not change those boundaries.
STOP 2 remains required before full qualification.
