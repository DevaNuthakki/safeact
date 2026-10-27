import { expect, test } from "@playwright/test";

import { accounts, overflowing, routeStoryFixtures, signIn, submitAs } from "./helpers";

// Every walkthrough page, the MCP guide and the actions list fit a 390px
// phone: no page-level horizontal scroll and nothing clipped behind one.
test.use({ viewport: { width: 390, height: 844 } });

async function fits(page: import("@playwright/test").Page, name: string) {
  const result = await overflowing(page);
  expect(result.scrollWidth, `${name}: page scroll width`).toBeLessThanOrEqual(result.innerWidth);
  expect(result.found, `${name}: elements outside the viewport or clipped`).toEqual([]);
}

test("walkthrough pages and the MCP guide fit a phone", async ({ page, context }) => {
  await routeStoryFixtures(context);
  for (const [path, heading] of [["/demo", "When a response is lost, did the action happen?"],
    ["/demo/results", "Measured results. Clear limits."], ["/mcp-guide", "Connect an AI agent"]] as const) {
    await page.goto(path);
    await expect(page.getByRole("heading", { name: heading })).toBeVisible();
    await fits(page, path);
  }
  await page.goto("/login");
  await page.getByLabel("Username").fill("demo-north-reviewer");
  await page.getByLabel("Password").fill(accounts().accounts["demo-north-reviewer"]!);
  await page.getByLabel("Password").press("Enter");
  await expect(page).toHaveURL(/\/demo$/);
  for (const [path, heading] of [["/demo/refund", "One $2,450.00 refund. A lost response."],
    ["/demo/agent", "The agent cannot mark its own work as done."],
    ["/demo/decision", "A human decision, bound to the evidence."]] as const) {
    await page.goto(path);
    await expect(page.getByRole("heading", { name: heading })).toBeVisible();
    await fits(page, path);
  }
  // The account menu opens inside the screen, beside the brand.
  await page.getByText("Reviewer", { exact: true }).click();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  await fits(page, "open account menu");
});

test("the actions list fits a phone", async ({ page }) => {
  await signIn(page, "demo-north-requester");
  await submitAs(page, { action_type: "refund_payment", provider_account_id: "mock-payments-account",
    payload: { charge_id: "ch_north_1001", amount_minor: 1200 } });
  await page.goto("/actions");
  await expect(page.getByRole("link", { name: "Refund payment" }).first()).toBeVisible();
  await fits(page, "/actions");
});
