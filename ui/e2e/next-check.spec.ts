import { expect, test } from "@playwright/test";

import { backend, signIn, submitAs } from "./helpers";

interface Detail {
  state: string;
  next_check: { kind: string; not_before: string | null; deadline: string | null };
}

// The console shows when the next verification check happens, from the
// same rules the database enforces (0018 pacing, 0019 next_check).
test("next check: a paced lookup while unknown, the bound after escalation, none when done",
  async ({ page }) => {
    await signIn(page, "demo-north-requester");

    // Late commit, record not searchable (demo fault script): UNKNOWN
    // with paced lookups, then escalated with a bounded lookup.
    const unknown = await submitAs(page, {
      action_type: "create_replacement_order", provider_account_id: "mock-crm-account",
      payload: { case_id: "case_north_2002", sku: "sku-replacement" },
    });
    await expect.poll(async () => (await backend<Detail>(page, `/actions/${unknown}`)).state,
      { timeout: 15_000 }).toMatch(/UNKNOWN|VERIFYING|ESCALATED_UNRESOLVED/);
    const first = await backend<Detail>(page, `/actions/${unknown}`);
    await page.goto(`/actions/${unknown}`);
    await expect(page.getByText("What happens next")).toBeVisible();
    if (first.state !== "ESCALATED_UNRESOLVED") {
      expect(first.next_check.kind).toBe("scheduled_lookup");
      expect(first.next_check.deadline).toBeTruthy();
      await expect(page.getByText(/^SafeAct will ask the provider again|^A person decides next\. SafeAct still checks/))
        .toBeVisible();
      await page.getByRole("tab", { name: "Technical details" }).click();
      await expect(page.getByText("Investigation deadline")).toBeVisible();
    }

    await expect.poll(async () => (await backend<Detail>(page, `/actions/${unknown}`)).state,
      { timeout: 30_000 }).toBe("ESCALATED_UNRESOLVED");
    const escalated = await backend<Detail>(page, `/actions/${unknown}`);
    expect(["post_escalation_lookup", "beyond_horizon"]).toContain(escalated.next_check.kind);
    await page.goto(`/actions/${unknown}`);
    await expect(page.getByText(/^A person decides next\. SafeAct still checks|^No more automatic checks\. A person decides next/)).toBeVisible();

    // A plain refund succeeds: nothing more will be checked.
    const done = await submitAs(page, {
      action_type: "refund_payment", provider_account_id: "mock-payments-account",
      payload: { charge_id: "ch_north_1001", amount_minor: 300 },
    });
    await expect.poll(async () => (await backend<Detail>(page, `/actions/${done}`)).state,
      { timeout: 15_000 }).toBe("SUCCEEDED");
    await page.goto(`/actions/${done}`);
    await expect(page.getByText("Nothing more will be checked or sent.")).toBeVisible();
  });
