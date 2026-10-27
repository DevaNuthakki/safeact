import { expect, test } from "@playwright/test";

import { backend, inPage, onSubmitForm, personContext, retainedRequestReleased, signIn, submitAs } from "./helpers";

interface Page { items: { operation_id: string; request_key: string }[] }

test("denied tenant access: another tenant's action is not found", async ({ browser }) => {
  const north = await personContext(browser, "demo-north-requester");
  const theirs = await submitAs(north.page, {
    action_type: "refund_payment", provider_account_id: "mock-payments-account",
    payload: { charge_id: "ch_north_1003", amount_minor: 100 },
  });
  const south = await personContext(browser, "demo-south-requester");
  await south.page.goto(`/actions/${theirs}`);
  await expect(south.page.getByText("Not found, or you cannot see it.")).toBeVisible();
  expect((await inPage(south.page, `/actions/${theirs}`)).status).toBe(404);
  const list = await backend<Page>(south.page, "/actions?limit=100");
  expect(list.items.map((i) => i.operation_id)).not.toContain(theirs);
  await north.context.close();
  await south.context.close();
});

test("duplicate submission: a double click and a refresh are one operation", async ({ page }) => {
  await signIn(page, "demo-north-requester");
  await page.goto("/submit");
  await onSubmitForm(page);
  const key = await page.getByText(/^ui-/).textContent();
  await page.getByLabel("Action", { exact: true }).selectOption("refund_payment");
  await page.getByLabel("Charge").selectOption({ label: "Charge #1004 · Payments (test)" });
  await page.getByLabel(/Amount/).fill("7.00");
  await page.getByRole("button", { name: "Submit" }).dblclick();
  await expect(page).toHaveURL(/\/actions\/[0-9a-f-]{36}$/);
  await retainedRequestReleased(page);

  const list = await backend<Page>(page, "/actions?limit=100");
  expect(list.items.filter((i) => i.request_key === key)).toHaveLength(1);

  // Refresh in the middle of a submit, deterministically: the request
  // reaches the server and is recorded, but its response never reaches
  // the page (lost on the way back). After the refresh the form keeps
  // its complete request, so retrying returns that same operation.
  await page.goto("/submit");
  await onSubmitForm(page);
  await expect(page.getByLabel("Action", { exact: true })).toBeVisible();
  const second = await page.getByText(/^ui-/).textContent();
  let recorded: string | undefined;
  await page.route("**/actions", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const response = await route.fetch();
    recorded = ((await response.json()) as { operation_id: string }).operation_id;
    // Hold the response: the page never learns the outcome.
  });
  await page.getByLabel("Action", { exact: true }).selectOption("refund_payment");
  await page.getByLabel("Charge").selectOption({ label: "Charge #1004 · Payments (test)" });
  await page.getByLabel(/Amount/).fill("7.01");
  await page.getByRole("button", { name: "Submit" }).click();
  await expect.poll(() => recorded, { timeout: 15_000 }).toBeTruthy();
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await page.reload();
  await expect(page.getByText(/^ui-/)).toHaveText(second!);

  await expect(page.getByText("$7.01", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Action", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Retry the same request" }).click();
  await expect(page).toHaveURL(new RegExp(`/actions/${recorded}$`));
  await expect(page.getByText("This request was already recorded; showing it.")).toBeVisible();
  await retainedRequestReleased(page);
  const after = await backend<Page>(page, "/actions?limit=100");
  expect(after.items.filter((i) => i.request_key === second)).toHaveLength(1);

  // Having seen the result, the next form is a new request.
  await page.goto("/submit");
  await onSubmitForm(page);
  await expect(page.getByLabel("Action", { exact: true })).toBeVisible();
  await expect(page.getByText(/^ui-/)).not.toHaveText(second!);
});

test("connection interruption: live updates recover and catch up", async ({ browser }) => {
  const requester = await personContext(browser, "demo-north-requester");
  const { page, context } = await personContext(browser, "demo-north-investigator");
  await expect(page.getByText("Auto-updating", { exact: true })).toBeVisible();

  // Cut the live channel; the console says so.
  await context.route("**/events**", (route) => route.abort("connectionreset"));
  await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  await page.reload();
  await expect(page.getByText(/Reconnecting|unavailable/)).toBeVisible();

  // Work happens while disconnected.
  const missed = await submitAs(requester.page, {
    action_type: "refund_payment", provider_account_id: "mock-payments-account",
    payload: { charge_id: "ch_north_1001", amount_minor: 123 },
  });

  // Restore the channel: the console reconnects and the list catches up.
  await context.unroute("**/events**");
  await expect(page.getByText("Auto-updating", { exact: true })).toBeVisible({ timeout: 60_000 });
  await expect(page.locator(`a[href="/actions/${missed}"]`)).toBeVisible();

  // An API outage mid-session shows an error state, then recovers.
  await context.route("**/actions?**", (route) => route.fulfill({
    status: 503, contentType: "application/json",
    body: JSON.stringify({ error: { code: "unavailable", message: "The service is temporarily unavailable." } }),
  }));
  await page.reload();
  await expect(page.getByRole("alert")).toContainText("temporarily unavailable");
  await context.unroute("**/actions?**");
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(page.locator(`a[href="/actions/${missed}"]`)).toBeVisible();
  await requester.context.close();
  await context.close();
});
