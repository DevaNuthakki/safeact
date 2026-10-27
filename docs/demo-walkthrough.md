# Three-minute demo walkthrough

The script for the blueprint's §12 demonstration, with the exact commands and clicks.
Everything here is DEMO ONLY: synthetic accounts, mock providers, local machine. The
optional last part uses Stripe **test mode**. Nothing in this demo touches real money.

What the demo shows:
1. A refund whose response is lost after the provider applied it. SafeAct verifies it
   and does not send it a second time.
2. A replacement order that the provider created but its search cannot see. SafeAct
   stays unresolved instead of guessing.
3. An agent cannot approve anything, and a stale human approval is refused. A
   permitted human evidence path then succeeds.
4. The comparison report: SafeAct against the robust baseline, with the unresolved
   cases and the known limits.

## Before you start

Choose one of the two ways to run the stack.

**A. Compose (closest to a fresh clone).** On a fresh checkout, write `.env` from
`.env.example`, set its two passwords, then:

```sh
chmod 600 .env
docker compose --env-file .env -f deploy/compose.stack.yaml up --build --wait
# console http://localhost:8080 ; MCP http://127.0.0.1:8002/mcp
(umask 077; docker compose --env-file .env -f deploy/compose.stack.yaml exec -T api \
    cat /state/demo-credentials > /tmp/safeact-demo-credentials)
```

**B. Development stack** (see the [README](../README.md#run-it-locally)):

```sh
.venv/bin/python -m deploy.demo.bootstrap      # resets safeact_demo, writes .demo-credentials
.venv/bin/python -m deploy.demo.run_stack      # API :8000, MCP 127.0.0.1:8002
cd ui && npm run dev                           # console http://127.0.0.1:5173
```

Seed the cases about a minute before you start. The order takes about
60 seconds to escalate at demo pace.

```sh
# Compose:
.venv/bin/python -m deploy.demo.walkthrough --api http://localhost:8080 \
    --credentials /tmp/safeact-demo-credentials --show-stale
# Development stack:
.venv/bin/python -m deploy.demo.walkthrough --show-stale
```

The seed (`deploy/demo/walkthrough.py`) uses only what any client may use: the API as
the demo people and the MCP server as the demo agent. It prints a JSON summary with a
console link for every case and exits 0 only if every beat came out as expected.

- **Keep the terminal output on screen;** it is part of beat 3.
- **The agent beat runs once per demo.** An agent gets one operation per target
  (SAT28, a disclosed limit). To seed again, rebuild the demo first: run the
  bootstrap again, or for Compose follow "Reset and reseed" in the
  [README](../README.md#run-it-locally).

Sign in as the person each beat names. Passwords are in the credentials file, on the
lines `demo-north-requester=…` and so on.

| Sign-in | Shown in the console as | Role |
|---|---|---|
| `demo-north-requester` | Alex Rivera, Support agent | requests actions |
| `demo-north-investigator` | Sam Chen, Investigator | proposes next steps |
| `demo-north-reviewer` / `demo-north-reviewer2` | Jordan Lee / Priya Patel, Reviewer | decides proposals |
| `demo-north-agent` (MCP key only) | Support assistant, AI agent | submits and investigates |

All of them work for **Northwind (demo)**; the `demo-south-*` accounts are **Southwind
(demo)**. The console shows these names from a small demo directory
(`ui/src/demo/directory.ts`) matched to fixed principal ids that the demo bootstrap
assigns, because the API returns people as ids and has no endpoint for another
person's name. A real deployment would resolve names through a tenant-scoped API.
Resources read as "Charge #1002", "Support case #2002" and "Customer #3002"; raw ids,
codes and versions are under **Technical details** on each page.

## The script (about three minutes)

### 1. Timeout after success: verified, not resent (about 40 s)

Sign in as **demo-north-requester** (Alex Rivera). Open the `1_timeout_after_success`
link, "Refund $2,450.00 on Charge #1002": a refund for a damaged order.

**What to say:**
- **The fault:** the provider applied the refund, and then its response was lost.
- **The naive path:** a client that retries on a timeout would have sent the refund
  again and refunded the customer twice, **$4,900.00**.
- **What SafeAct did:** it checked with the provider instead and made exactly one
  attempt.
- **The measured version:** across the 1,200-scenario qualification, the bounded
  naive-retry baseline (B0) produced 371 prohibited duplicate effects
  ([results](../README.md#fault-injected-qualification-primary-run-v1-2026-10-25)).
- **Demo-only simplification:** the mock provider models no charge balance, so it
  does not check that a refund is at most the original charge.

- **Status:** the badge in the summary card says **Succeeded**; the progress bar shows
  Recorded → Sent → Checked → Succeeded.
- **Timeline tab:** sent, no answer, checked with the provider, confirmed. The
  provider applied the refund, then the response was lost. SafeAct recorded UNKNOWN, a
  real state, instead of calling it a failure (the state names are under each step's
  Technical details: CLAIMED → DISPATCHING → UNKNOWN → VERIFYING → SUCCEEDED).
- **Evidence tab:** exactly **one** attempt. The reconciler's lookup found the
  provider's record for this operation's idempotency key, and that evidence is what
  marks it a success. Nothing was dispatched a second time.
- **Technical details tab, "What the connector contract assumes":** a payments
  contract with a native key, a stated retention period and a lookup.

### 2. Late commit, empty lookup: unresolved on purpose (about 40 s)

Open the `2_late_commit_empty_lookup` link, "Replacement order for Support case #2002".

- **Status:** the badge says **Needs review** (ESCALATED_UNRESOLVED).
- **Timeline tab:** after the lost response, the repeated provider lookups are folded
  into one step ("No decisive answer from N provider lookups"). Every CRM search came
  back empty.
- **Why it stays unresolved:** the mock CRM really did create the order, but its
  search cannot see it yet. An empty search is not proof that nothing happened, so
  SafeAct neither resends nor calls it a failure. It escalates with its evidence
  attached; the Overview's **What happens next** says when the reconciler looks again.

### 3. No self-approval, no stale approval, then the permitted path (about 70 s)

**The agent**, from the terminal output, `3_agent`:
- **Tools:** the guarded MCP tools are listed, and none of them approves anything.
- **Its own work:** the agent submitted the refund confirmation email for
  `cust_north_3002`. The email was sent and its response lost, so SafeAct escalated it
  at once and never resends it.
- **The refused attestation:** the agent tried `propose_resolution` with
  `ATTEST_APPLIED` and was refused (`invalid_arguments`). An agent may propose only
  REVERIFY or PROTECTED_RETRY, and only a human reviewer decides, in the console.

**A stale human approval.** Sign in as **demo-north-reviewer** (Jordan Lee) and open
the `goes_stale` link (or **Reviews**, then "Confirm replacement order #2002 was
created"). Sam Chen, the investigator, proposed attesting the order as created. Since
then, the reconciler's next lookup changed the order's evidence.
- **In the console:** the page says *This proposal is stale. It cannot be approved.*
  and the comparison flags "New evidence recorded since".
- **In the terminal (`stale_approval`):** approving anyway with the binding the
  reviewer was shown gets **409 `stale_approval`** (`evidence_changed`). The server
  refuses it, not just the console.

**The permitted path.** Still as the reviewer, open the `approve_this` link, "Confirm
the refund email was delivered". The investigator attested the agent's email as
delivered, with the provider's reference `msg_demo_delivered_1`.
1. Show **What you saw vs current** ("No new evidence"); open **Technical details**
   for the version, state and evidence digest the approval is bound to.
2. Click **Approve**.
3. Open the operation with **Inspect the evidence**. It is **Succeeded**, marked
   *attested by a reviewer, not verified by the provider*, still with one attempt.

The reviewer is neither the proposer nor the requester, and decides once.

### 4. The comparison report (about 30 s)

Open the [README results](../README.md#results) or the
[full-run review](phase-e/qual-v1/full/REVIEW.md). Show these:
- **The headline:** 1,200 seeded scenarios × 5 arms. SafeAct had 0 prohibited
  duplicates, 0 orphans and 0 lost intents.
- **B3, the robust hand-built baseline,** is also safe, and SafeAct completes more of
  the achievable work: 924/937 against 841/874.
- **SafeAct's 13 misses:** F16 storms and an outage spent the fixed attempt budget.
  SafeAct then ended in proven non-application or escalated, with 0 effects.
- **The unresolved cases:** F09 retention expiry is declared not completable.
- **The limits:** one laptop, mock providers; it is not exactly-once and not
  production.

## Optional: the same refund against Stripe test mode

This part goes to Stripe **test mode** through the existing adapter and all of its
gates:
- a restricted `rk_test_` key in `~/.config/safeact/stripe.key` (mode 600, created by
  the author; never printed or logged);
- `SAFEACT_ALLOW_STRIPE_SANDBOX=1`, and never under CI;
- any live-mode object stops the stack.

It runs on the development stack only. The Compose stack and the mock demo are
unchanged.

```sh
export SAFEACT_ALLOW_STRIPE_SANDBOX=1
.venv/bin/python -m deploy.demo.stripe_demo setup     # two test PaymentIntents, 20.00 each;
                                                       # prints their dashboard links
.venv/bin/python -m deploy.demo.bootstrap --stripe     # + the stripe-test contract and grants
.venv/bin/python -m deploy.demo.run_stack --stripe     # API :8000 (console: npm run dev)
```

As **demo-north-requester**, go to **Submit** and choose **Refund a Stripe test
payment**:
1. **Normal refund.** Pick the PaymentIntent marked `normal` in the setup output (the
   option "Stripe test payment ···" ends with its last four characters) and
   enter amount `5.00` (dollars; sent as 500 cents). It ends **Succeeded** from
   Stripe's answer, with one attempt. On the overview, **At Stripe** says what that
   means: Stripe accepted the request and created the refund. It also shows the refund
   status Stripe reported at the time (for example *Pending* or *Succeeded*), which is
   not a live value.
2. **Lost response.** Submit again with the `response_lost` PaymentIntent and amount
   `5.00`. Stripe creates the refund, then the demo harness drops the response before
   SafeAct sees it.
   - **Timeline:** UNKNOWN → VERIFYING → SUCCEEDED, with one attempt.
   - **How it was verified:** the reconciler listed the refunds and matched this
     operation's metadata. SafeAct did not send the refund again.

Then check Stripe's own record:

```sh
.venv/bin/python -m deploy.demo.stripe_demo check     # PASS: exactly one refund per PaymentIntent
```

Open the dashboard links from `setup`. Each PaymentIntent shows **exactly one**
refund.

Disclosed limits:
- **What "applied" means:** Stripe accepted the request and a refund object exists.
  The refund's own status is shown as Stripe reported it at response time. A refund
  that later fails asynchronously, and settlement to the customer, are outside what
  SafeAct verifies (no webhooks).
- **The drop is staged:** the dropped response is harness behaviour, fixed by the
  fixture file and never chosen through the API.
- **Run once against Stripe test mode (2026-10-25):**
  - **Normal refund:** `pi_3UUbA1DnpP0MDY8U4RYP3jpm`, SUCCEEDED from Stripe's answer.
    Refund POST `req_QbeYYZ6EmdU39C`; 1 refund at Stripe.
  - **Dropped-response refund:** `pi_3UUbA2DnpP0MDY8U0R6eGrx5`, UNKNOWN → VERIFYING →
    SUCCEEDED by lookup. One refund POST `req_BiLvrB1WPbI0PE`, lookup
    `req_B3lA7qE8N5uAg5`; **exactly 1 refund at Stripe**.
  - **Check:** `check` passed. Full record:
    [stripe-demo-v1](phase-e/stripe-demo-v1/REVIEW.md). The offline test against the
    documented-behaviour fake is `tests/e2e_api/test_stripe_demo.py`.

## Reset

- **Compose:** `docker compose --env-file .env -f deploy/compose.stack.yaml down -v`.
- **Development stack:** run `.venv/bin/python -m deploy.demo.bootstrap` again (add
  `--stripe` to keep the Stripe grants). The bootstrap regenerates every demo
  credential.
