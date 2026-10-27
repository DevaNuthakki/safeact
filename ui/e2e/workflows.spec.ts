import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

import { backend, inPage, signIn } from "./helpers";

interface Workflow {
  status: string;
  steps: { step: string; operation_id: string; state: string; compensates_operation_id: string | null }[];
}

test("replacement workflow: submit, follow evidence and refresh", async ({ page }) => {
  await signIn(page, "demo-north-requester");
  await page.getByRole("link", { name: "Send a replacement" }).click();
  await page.getByLabel("Customer to charge").selectOption({ label: "Customer #3001 · Payments (test)" });
  await page.getByLabel("Support case").selectOption({ label: "Support case #2001 · CRM (test)" });
  await page.getByLabel("Replacement SKU").fill("sku-browser-demo");
  await page.getByLabel("Shipping fee (USD)").fill("5.00");
  type AxePage = ConstructorParameters<typeof AxeBuilder>[0]["page"];
  const formAxe = await new AxeBuilder({ page: page as unknown as AxePage }).withTags(["wcag2a", "wcag2aa"]).analyze();
  expect(formAxe.violations.filter((v) => ["serious", "critical"].includes(v.impact ?? ""))).toEqual([]);
  await page.getByRole("button", { name: "Start replacement workflow" }).click();
  await expect(page).toHaveURL(/\/workflows\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("heading", { name: "Replacement completed" })).toBeVisible();
  const path = new URL(page.url()).pathname;
  await page.screenshot({ path: test.info().outputPath("replacement-completed.png"), fullPage: true });
  const workflow = await backend<Workflow>(page, path);
  expect(workflow.status).toBe("COMPLETED");
  expect(workflow.steps.map((s) => s.step).sort()).toEqual(["charge", "order"]);
  const detailAxe = await new AxeBuilder({ page: page as unknown as AxePage }).withTags(["wcag2a", "wcag2aa"]).analyze();
  expect(detailAxe.violations.filter((v) => ["serious", "critical"].includes(v.impact ?? ""))).toEqual([]);
  await page.reload();
  await expect(page.getByRole("heading", { name: "Replacement completed" })).toBeVisible();
  await page.getByRole("link", { name: "Inspect charge evidence" }).click();
  await expect(page.getByRole("link", { name: "A replacement for a customer" })).toHaveAttribute("href", path);
});

test("replacement workflow: a lost admission reply survives reload with the same intent", async ({ page, context }) => {
  await signIn(page, "demo-north-requester");
  let originalId: string | undefined;
  await context.route("**/workflows", async (route) => {
    if (route.request().method() !== "POST" || originalId) return route.fallback();
    const accepted = await route.fetch();
    expect(accepted.status()).toBe(201);
    originalId = (await accepted.json() as { workflow_id: string }).workflow_id;
    // The server committed admission, but the browser did not receive it.
    await route.fulfill({ status: 503, contentType: "application/json",
      body: JSON.stringify({ error: { code: "unavailable", message: "Admission reply unavailable." } }) });
  });
  await page.goto("/workflows/new");
  await page.getByLabel("Customer to charge").selectOption({ label: "Customer #3001 · Payments (test)" });
  await page.getByLabel("Support case").selectOption({ label: "Support case #2001 · CRM (test)" });
  await page.getByLabel("Replacement SKU").fill("sku-lost-admission");
  await page.getByLabel("Shipping fee (USD)").fill("7.00");
  await page.getByRole("button", { name: "Start replacement workflow" }).click();
  await expect(page.getByRole("alert")).toContainText("Admission reply unavailable");
  await page.reload();
  await expect(page.getByText("sku-lost-admission", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Retry the same request" }).click();
  await expect(page).toHaveURL(new RegExp(`/workflows/${originalId}$`));
  await expect(page.getByRole("heading", { name: "Replacement completed" })).toBeVisible();
  const workflow = await backend<Workflow>(page, `/workflows/${originalId}`);
  const charge = workflow.steps.find((s) => s.step === "charge")!;
  const detail = await backend<{ payload: { amount_minor: number }; attempts: unknown[] }>(
    page, `/actions/${charge.operation_id}`);
  expect(detail.payload.amount_minor).toBe(700);
  expect(detail.attempts).toHaveLength(1);
});

for (const scenario of [
  { supportCase: "case_north_2004", status: "COMPENSATED_VERIFIED", title: "Refund confirmed" },
  { supportCase: "case_north_2002", status: "UNRESOLVED", title: "Replacement needs investigation" },
]) {
  test(`replacement workflow: ${scenario.status} is explained without false completion`, async ({ page }) => {
    await signIn(page, "demo-north-requester");
    const body = { request_key: `workflow-browser-${crypto.randomUUID()}`,
      customer_id: "cust_north_3001", case_id: scenario.supportCase, sku: "sku-browser-demo",
      fee_minor: 500, payments_account_id: "mock-payments-account", crm_account_id: "mock-crm-account" };
    const admission = await inPage(page, "/workflows", { method: "POST", body });
    expect(admission.status, admission.body).toBe(201);
    const { workflow_id } = JSON.parse(admission.body) as { workflow_id: string };
    const path = `/workflows/${workflow_id}`;
    await page.goto(path);
    await expect(page.getByRole("heading", { name: scenario.title })).toBeVisible();
    const workflow = await backend<Workflow>(page, path);
    expect(workflow.status).toBe(scenario.status);
    await page.screenshot({ path: test.info().outputPath(`${scenario.status.toLowerCase()}.png`), fullPage: true });
    if (scenario.status === "COMPENSATED_VERIFIED") {
      const charge = workflow.steps.find((s) => s.step === "charge")!;
      const refund = workflow.steps.find((s) => s.step === "compensation")!;
      expect(refund.compensates_operation_id).toBe(charge.operation_id);
      expect(refund.operation_id).not.toBe(charge.operation_id);
      await expect(page.getByText(/replacement was not completed/)).toBeVisible();
      await expect(page.getByRole("link", { name: "Inspect refund evidence" })).toBeVisible();
    } else {
      expect(workflow.steps.some((s) => s.step === "compensation")).toBe(false);
      await expect(page.getByText(/a refund is withheld/)).toBeVisible();
    }
    const replay = await inPage(page, "/workflows", { method: "POST", body });
    expect(replay.status).toBe(200);
    expect(JSON.parse(replay.body).workflow_id).toBe(workflow_id);
    const denied = await inPage(page, "/workflows", {
      method: "POST", body: { ...body, sku: "changed-intent" },
    });
    expect(denied.status).toBe(409);
  });
}
