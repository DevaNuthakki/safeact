import { expect, test } from "@playwright/test";

import { accounts, noSeriousViolations, personContext, submitAs } from "./helpers";

// Light is the default; dark is chosen in the header and remembered per
// browser. Both themes are scanned on every page.
for (const theme of ["light", "dark"] as const) {
  test(`every page has no serious accessibility violations (${theme} theme)`, async ({ browser, page }) => {
    const remember = (t: string) => { try { localStorage.setItem("safeact-theme", t); } catch { /* none */ } };
    await page.context().addInitScript(remember, theme);
    await page.goto("/login");
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await noSeriousViolations(page, `${theme} login`);

    const requester = await personContext(browser, "demo-north-requester");
    await requester.context.addInitScript(remember, theme);
    const id = await submitAs(requester.page, {
      action_type: "refund_payment", provider_account_id: "mock-payments-account",
      payload: { charge_id: "ch_north_1001", amount_minor: 200 },
    });
    for (const [name, path] of [["actions", "/actions"], ["submit", "/submit"],
      ["replacement", "/workflows/new"], ["detail", `/actions/${id}`],
      ["detail timeline", `/actions/${id}?tab=timeline`], ["detail evidence", `/actions/${id}?tab=evidence`],
      ["detail technical", `/actions/${id}?tab=technical`]]) {
      await requester.page.goto(path!);
      await expect(requester.page.locator("main h1")).toBeVisible();
      await expect(requester.page.locator("html")).toHaveAttribute("data-theme", theme);
      await noSeriousViolations(requester.page, `${theme} ${name}`);
    }
    const reviewer = await personContext(browser, "demo-north-reviewer");
    await reviewer.context.addInitScript(remember, theme);
    await reviewer.page.goto("/proposals?status=all");
    await expect(reviewer.page.locator("main h1")).toBeVisible();
    await noSeriousViolations(reviewer.page, `${theme} proposals`);
    await requester.context.close();
    await reviewer.context.close();
  });
}

test("keyboard only: skip link, sign-in and submit", async ({ page }) => {
  await page.goto("/login?console=1");
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("Username")).toBeFocused();
  await page.keyboard.type("demo-north-requester");
  await page.keyboard.press("Tab");
  await page.keyboard.type(accounts().accounts["demo-north-requester"]!);
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Actions" })).toBeVisible();

  // The summary tiles, the theme button and the user menu are reachable
  // and operable from the keyboard.
  const tile = page.getByRole("button", { name: /Needs review/ });
  await tile.focus();
  await page.keyboard.press("Enter");
  await expect(tile).toHaveAttribute("aria-pressed", "true");
  await expect(page).toHaveURL(/group=needs_review/);
  const dark = page.getByRole("button", { name: "Dark theme" });
  await dark.focus();
  await expect(dark).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(dark).toHaveAttribute("aria-pressed", "true");
  await page.keyboard.press("Enter");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.keyboard.press("Tab");
  const account = page.getByRole("button", { name: /^Account:/ });
  await expect(account).toBeFocused();

  // Increased desktop text size must reflow without hiding the main actions.
  await page.evaluate(() => { document.documentElement.style.fontSize = "200%"; });
  await expect(page.getByRole("link", { name: "New action" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.evaluate(() => { document.documentElement.style.fontSize = ""; });
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: "Sign out" })).toBeHidden();
  await expect(account).toBeFocused();

  await page.goto("/submit");
  await expect(page.getByRole("heading", { name: "Submit an action" })).toBeVisible();
  // The skip link is the first focusable element and moves focus to
  // the page content.
  const first = await page.evaluate(() => {
    const focusable = document.querySelector<HTMLElement>(
      "a[href], button, input, select, textarea, [tabindex]:not([tabindex='-1'])",
    );
    return focusable?.textContent;
  });
  expect(first).toBe("Skip to main content");
  const skip = page.getByRole("link", { name: "Skip to main content" });
  await skip.focus();
  await expect(skip).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("main")).toBeFocused();
  // Every control in the form is reachable and labelled.
  const action = page.getByLabel("Action", { exact: true });
  await action.focus();
  await action.selectOption("refund_payment");
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("Charge")).toBeFocused();
  await page.getByLabel("Charge").selectOption({ index: 1 });
  await page.keyboard.press("Tab");
  await expect(page.getByLabel(/Amount/)).toBeFocused();
  await page.keyboard.type("42");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Submit" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/actions\/[0-9a-f-]{36}$/);
});
