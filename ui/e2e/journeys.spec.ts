import { expect, test } from "@playwright/test";

import {
  backend, freezeProposalScreen, inPage, noSeriousViolations, onSubmitForm, personContext, signIn, submitAs,
  waitForState,
} from "./helpers";

interface Detail { state: string; seq: number; success_provenance: string | null }

test("journey 1: submit once, follow live, recover after a refresh", async ({ page }) => {
  await signIn(page, "demo-north-requester");
  await page.getByRole("link", { name: "New action" }).click();
  await onSubmitForm(page);
  await page.getByLabel("Action", { exact: true }).selectOption("refund_payment");
  await page.getByLabel("Charge").selectOption({ label: "Charge #1001 · Payments (test)" });
  await page.getByLabel(/Amount/).fill("15.00");
  await page.getByRole("button", { name: "Submit" }).click();

  await expect(page).toHaveURL(/\/actions\/[0-9a-f-]{36}$/);
  const operationId = page.url().split("/").at(-1)!;
  // Live: the badge turns to Succeeded without a reload.
  await expect(page.getByText("Auto-updating", { exact: true })).toBeVisible();
  await expect(page.getByText("Succeeded", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("tabpanel", { name: "Overview" }).getByText("Verified with the provider", { exact: true })).toBeVisible();

  await page.reload();
  await expect(page.getByText("Succeeded", { exact: true }).first()).toBeVisible();
  const detail = await backend<Detail>(page, `/actions/${operationId}`);
  expect(detail.state).toBe("SUCCEEDED");
  expect(detail.success_provenance).toBe("provider_verified");
});

test("journey 2: investigate a timeout after success and a late commit", async ({ browser }) => {
  const requester = await personContext(browser, "demo-north-requester");
  const verified = await submitAs(requester.page, {
    action_type: "refund_payment", provider_account_id: "mock-payments-account",
    payload: { charge_id: "ch_north_1002", amount_minor: 900 },
  });
  const late = await submitAs(requester.page, {
    action_type: "create_replacement_order", provider_account_id: "mock-crm-account",
    payload: { case_id: "case_north_2002", sku: "sku-e2e" },
  });

  const { page } = await personContext(browser, "demo-north-investigator");
  await waitForState(page, verified, ["SUCCEEDED"]);
  await waitForState(page, late, ["ESCALATED_UNRESOLVED"]);

  const unresolved = page.getByLabel("Only unknown or needing review");
  await unresolved.click();
  await expect(unresolved).toBeChecked();
  await expect(page.getByRole("link", { name: "Create replacement order" }).first()).toBeVisible();
  await page.goto(`/actions/${late}?tab=evidence`);
  await expect(page.getByText("Outcome unknown.")).toBeVisible();
  // The hero's state badge and the stepper name it; the evidence tab shows why.
  await expect(page.locator(".hero .badge").getByText("Needs review", { exact: true })).toBeVisible();
  await expect(page.locator(".stepper").getByText("Needs review", { exact: true })).toBeVisible();
  await expect(page.locator(".evidence").getByText("Not found").first()).toBeVisible();

  await page.goto(`/actions/${verified}`);
  await expect(page.getByRole("tabpanel", { name: "Overview" }).getByText("Verified with the provider", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "Timeline" }).click();
  await expect(page.getByText("No response from the provider, checking before doing anything else.")).toBeVisible();
  await requester.context.close();
});

test("journey 3: propose, refuse a stale second decision, approve once", async ({ browser }) => {
  const requester = await personContext(browser, "demo-north-requester");
  const email = await submitAs(requester.page, {
    action_type: "send_refund_confirmation", provider_account_id: "mock-email-account",
    payload: { customer_id: "cust_north_3002", template: "refund_done" },
  });

  const investigator = await personContext(browser, "demo-north-investigator");
  await waitForState(investigator.page, email, ["ESCALATED_UNRESOLVED"]);
  await investigator.page.goto(`/actions/${email}`);
  await expect(investigator.page.getByRole("heading", { name: "What happened" })).toBeVisible();
  await investigator.page.screenshot({ path: test.info().outputPath("investigator.png"), fullPage: true });
  await investigator.page.getByLabel("Action", { exact: true }).selectOption("ATTEST_APPLIED");
  await investigator.page.getByLabel(/Provider reference/).fill("msg_delivered_ui_1");
  await investigator.page.getByLabel("Why").fill("Delivery receipt visible in the provider console.");
  await investigator.page.getByRole("button", { name: "Propose" }).click();
  await expect(investigator.page).toHaveURL(/\/proposals\/[0-9a-f-]{36}$/);
  const proposalPath = new URL(investigator.page.url()).pathname;

  // Two reviewers open the same proposal. The second screen is frozen
  // before the first decision, so the live update that decision causes
  // cannot re-render it: it keeps offering Approve, as a slow screen
  // would, and the API must refuse it.
  const proposalId = proposalPath.split("/").at(-1)!;
  const first = await personContext(browser, "demo-north-reviewer");
  const second = await personContext(browser, "demo-north-reviewer2");
  await freezeProposalScreen(second.context, proposalId);
  await first.page.goto(proposalPath);
  await second.page.goto(proposalPath);
  await expect(second.page.getByRole("button", { name: "Approve" })).toBeVisible();
  await expect(first.page.getByRole("button", { name: "Approve" })).toBeVisible();
  await first.page.screenshot({ path: test.info().outputPath("review-proposal.png"), fullPage: true });
  await noSeriousViolations(first.page, "open proposal");
  await first.page.getByRole("button", { name: "Approve" }).click();
  await expect(first.page.getByText("Approved.")).toBeVisible();
  // The second screen still offers Approve; the API refuses it.
  await second.page.getByRole("button", { name: "Approve" }).click();
  await expect(second.page.getByText("Another reviewer already decided this proposal.")).toBeVisible();

  await waitForState(first.page, email, ["SUCCEEDED"]);
  await first.page.goto(`/actions/${email}`);
  await expect(first.page.getByText(/attested by a reviewer, not verified/)).toBeVisible();
  await first.page.getByRole("tab", { name: /^Evidence/ }).click();
  await expect(first.page.getByText("Reviewer attested", { exact: true })).toBeVisible();
  const proposals = await backend<{ items: { status: string }[] }>(first.page, "/proposals?status=approved");
  expect(proposals.items.length).toBeGreaterThan(0);

  for (const person of [requester, investigator, first, second]) await person.context.close();
});

test("journey 3: a screen left open while evidence changes cannot approve", async ({ browser }) => {
  test.setTimeout(150_000);
  const requester = await personContext(browser, "demo-north-requester");
  const crm = await submitAs(requester.page, {
    action_type: "create_replacement_order", provider_account_id: "mock-crm-account",
    payload: { case_id: "case_north_2002", sku: "sku-stale" },
  });
  const investigator = await personContext(browser, "demo-north-investigator");
  await waitForState(investigator.page, crm, ["ESCALATED_UNRESOLVED"]);
  // Past the contract's post-escalation horizon (30 s at e2e pace) no
  // automatic lookup runs, so the evidence stays still until we change
  // it below.
  const escalated = await backend<{ transitions: { to_state: string; created_at: string }[] }>(
    investigator.page, `/actions/${crm}`);
  const at = Date.parse(escalated.transitions.findLast((t) => t.to_state === "ESCALATED_UNRESOLVED")!.created_at);
  await investigator.page.waitForTimeout(Math.max(0, at + 32_000 - Date.now()));
  const attest = await inPage(investigator.page, `/actions/${crm}/proposals`, {
    method: "POST",
    body: { action: "ATTEST_APPLIED", rationale: "Seen in CRM.", attested_reference: "crm_ui_1" },
  });
  expect(attest.status, attest.body).toBe(201);
  const { proposal_id } = JSON.parse(attest.body) as { proposal_id: string };

  // The reviewer opens it on a screen that cannot refresh.
  const reviewer = await personContext(browser, "demo-north-reviewer");
  await freezeProposalScreen(reviewer.context, proposal_id);
  await reviewer.page.goto(`/proposals/${proposal_id}`);
  await expect(reviewer.page.getByRole("button", { name: "Approve" })).toBeVisible();

  // Meanwhile another reviewer approves one extra lookup, which records
  // new evidence.
  const reverify = await inPage(investigator.page, `/actions/${crm}/proposals`, {
    method: "POST", body: { action: "REVERIFY", rationale: "One more look." },
  });
  expect(reverify.status, reverify.body).toBe(201);
  const other = await personContext(browser, "demo-north-reviewer2");
  const { proposal_id: reverifyId, binding_digest } = JSON.parse(reverify.body) as
    { proposal_id: string; binding_digest: string };
  const approved = await inPage(other.page, `/proposals/${reverifyId}/decision`, {
    method: "POST",
    body: { decision: "approve", binding_digest, decision_key: crypto.randomUUID() },
  });
  expect(approved.status, approved.body).toBe(200);
  await expect.poll(async () => (await backend<{ status: string }>(
    other.page, `/proposals/${proposal_id}`)).status, { timeout: 30_000 }).toBe("stale");

  // The old screen tries to approve: refused, explained, nothing changed.
  await reviewer.page.getByRole("button", { name: "Approve" }).click();
  await expect(reviewer.page.getByText(/Not approved: new evidence was recorded/)).toBeVisible();
  await reviewer.page.screenshot({ path: test.info().outputPath("stale-proposal.png"), fullPage: true });
  expect((await backend<Detail>(other.page, `/actions/${crm}`)).state).toBe("ESCALATED_UNRESOLVED");

  for (const person of [requester, investigator, reviewer, other]) await person.context.close();
});
