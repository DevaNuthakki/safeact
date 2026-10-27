import { expect, test } from "@playwright/test";

import { accounts, noSeriousViolations, routeStoryFixtures } from "./helpers";

test("portfolio introduction, results and evidence are readable without signing in", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/demo$/);
  await expect(page.getByRole("heading", { name: "When a response is lost, did the action happen?" })).toBeVisible();
  await page.getByRole("link", { name: "Results & limits", exact: true }).click();
  await expect(page.getByText("861 / 874", { exact: true })).toBeVisible();
  await expect(page.getByText("841 / 874", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Read workflow documentation" }).click();
  await expect(page.getByRole("heading", { name: "Conditional refund workflow" })).toBeVisible();
  await expect(page.locator("pre")).toContainText("Customer-support walkthrough");
  await noSeriousViolations(page, "bundled evidence");
});

// UI fixtures isolate presentation from the demo server's expiring proposal IDs.
// Authentication is real; the real decision API and MCP boundaries are covered
// by journeys.spec.ts and the API walkthrough integration test respectively.
for (const theme of ["light", "dark"] as const) {
  test(`five connected chapters, one reviewer sign-in, no automatic decisions (${theme})`, async ({ page, context }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await context.addInitScript((value) => localStorage.setItem("safeact-theme", value), theme);
    await routeStoryFixtures(context);
    const decisions: string[] = [];
    page.on("request", (request) => { if (request.method() === "POST" && request.url().endsWith("/decision")) decisions.push(request.url()); });
    await page.goto("/demo");
    await noSeriousViolations(page, `${theme} story introduction`);
    await page.getByRole("link", { name: "Start with the refund" }).click();
    await page.getByLabel("Username").fill("demo-north-reviewer");
    await page.getByLabel("Password").fill(accounts().accounts["demo-north-reviewer"]!);
    await page.getByLabel("Password").press("Enter");
    await expect(page).toHaveURL(/\/demo\/refund$/);
    await expect(page.getByRole("heading", { name: "One $2,450.00 refund. A lost response." })).toBeVisible();
    await noSeriousViolations(page, `${theme} refund`);
    await page.getByRole("link", { name: "Next: The agent case" }).click();
    await expect(page.getByRole("heading", { name: "Captured MCP response" })).toBeVisible();
    await expect(page.getByText(/not a live event in the REST/)).toBeVisible();
    await noSeriousViolations(page, `${theme} agent`);
    await page.getByRole("link", { name: "Next: Human decision" }).click();
    const email = page.getByRole("region", { name: "The same email · Customer #3002" });
    await expect(email.getByRole("button", { name: "Approve attestation" })).toBeEnabled();
    await expect(page.getByRole("region", { name: "The order · Case #2002" }).getByRole("button", { name: "Approve attestation" })).toBeDisabled();
    await noSeriousViolations(page, `${theme} human decision`);
    await page.evaluate(() => { document.documentElement.style.fontSize = "200%"; });
    await expect(email.getByRole("button", { name: "Approve attestation" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.evaluate(() => { document.documentElement.style.fontSize = ""; });
    await page.getByRole("link", { name: "Skip to main content" }).focus();
    await page.keyboard.press("Enter");
    await expect(page.locator("main")).toBeFocused();
    await page.getByRole("link", { name: "Next: Results & limits" }).click();
    await expect(page.getByRole("heading", { name: "Measured results. Clear limits." })).toBeVisible();
    await noSeriousViolations(page, `${theme} results`);
    expect(decisions).toEqual([]);
  });
}
