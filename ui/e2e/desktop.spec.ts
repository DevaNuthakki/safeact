import { expect, test } from "@playwright/test";

import { backend, noSeriousViolations, signIn } from "./helpers";

test("desktop: MCP guidance is readable without exposing or managing keys", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/login");
  await page.screenshot({ path: test.info().outputPath("01-sign-in.png"), fullPage: true });
  await page.getByRole("link", { name: "Open the MCP guide" }).click();
  await expect(page.getByRole("heading", { name: "Connect an AI agent" })).toBeVisible();
  for (const tool of ["submit_action", "get_action_status", "inspect_evidence", "request_verification", "propose_resolution"]) {
    await expect(page.getByRole("cell", { name: tool, exact: true })).toBeVisible();
  }
  await expect(page.getByText(/Agents cannot approve proposals/)).toBeVisible();
  await expect(page.getByText(/MCP supports individual actions/)).toBeVisible();
  await expect(page.locator("input")).toHaveCount(0);
  await noSeriousViolations(page, "public MCP guide");
  await page.screenshot({ path: test.info().outputPath("02-mcp-guide.png"), fullPage: true });
  await signIn(page, "demo-north-requester");
  await page.getByRole("link", { name: "MCP guide", exact: true }).click();
  await expect(page.getByRole("link", { name: "All actions" })).toBeVisible();
  await noSeriousViolations(page, "signed-in MCP guide");
});

test("desktop: a lost action admission reply recovers the original request after reload", async ({ page, context }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await signIn(page, "demo-north-requester");
  const submitted: unknown[] = [];
  let originalId: string | undefined;
  await context.route("**/actions", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    submitted.push(route.request().postDataJSON());
    if (originalId) return route.fallback();
    const accepted = await route.fetch();
    expect(accepted.status()).toBe(201);
    originalId = (await accepted.json() as { operation_id: string }).operation_id;
    await route.fulfill({ status: 503, contentType: "application/json",
      body: JSON.stringify({ error: { code: "unavailable", message: "Admission reply unavailable." } }) });
  });
  await page.getByRole("link", { name: "New action" }).click();
  await page.getByLabel("Action", { exact: true }).selectOption("refund_payment");
  await page.getByLabel("Charge", { exact: true }).selectOption({ label: "Charge #1002 · Payments (test)" });
  await page.getByLabel("Amount (USD)").fill("2450.00");
  await page.screenshot({ path: test.info().outputPath("03-new-action.png"), fullPage: true });
  await page.getByRole("button", { name: "Submit", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Admission reply unavailable");
  await page.reload();
  await expect(page.getByRole("heading", { name: "Request retained in this browser" })).toBeVisible();
  await expect(page.getByText("$2,450.00", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Amount (USD)")).toHaveCount(0);
  await noSeriousViolations(page, "retained action submission");
  await page.screenshot({ path: test.info().outputPath("04-retained-request.png"), fullPage: true });
  await page.getByRole("button", { name: "Retry the same request" }).click();
  await expect(page).toHaveURL(new RegExp(`/actions/${originalId}$`));
  await expect(page.getByRole("tabpanel", { name: "Overview" }).getByText("Verified with the provider", { exact: true })).toBeVisible();
  const detail = await backend<{ attempts: unknown[]; payload: { amount_minor: number } }>(page, `/actions/${originalId}`);
  expect(detail.attempts).toHaveLength(1);
  expect(detail.payload.amount_minor).toBe(245000);
  expect(submitted).toHaveLength(2);
  expect(submitted[1]).toEqual(submitted[0]);
  await page.screenshot({ path: test.info().outputPath("05-action-overview.png"), fullPage: true });
  await page.getByRole("tab", { name: "Timeline", exact: true }).click();
  await page.screenshot({ path: test.info().outputPath("06-action-timeline.png"), fullPage: true });
  await page.getByRole("link", { name: "All actions", exact: true }).click();
  await expect(page.getByRole("columnheader", { name: "Requested by" })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("07-actions.png"), fullPage: true });
});

test("desktop: unavailable browser storage blocks sending and explains recovery", async ({ page }) => {
  await signIn(page, "demo-north-requester");
  await page.getByRole("link", { name: "New action" }).click();
  await page.getByLabel("Action", { exact: true }).selectOption("refund_payment");
  await page.getByLabel("Charge", { exact: true }).selectOption({ label: "Charge #1001 · Payments (test)" });
  await page.getByLabel("Amount (USD)").fill("15.00");
  let posts = 0;
  page.on("request", (request) => { if (request.method() === "POST" && new URL(request.url()).pathname === "/actions") posts += 1; });
  await page.evaluate(() => {
    Storage.prototype.setItem = () => { throw new DOMException("Storage unavailable", "QuotaExceededError"); };
  });
  await page.getByRole("button", { name: "Submit", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("No request was sent by this attempt");
  expect(posts).toBe(0);
  await noSeriousViolations(page, "storage refusal");
});
