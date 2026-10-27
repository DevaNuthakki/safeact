import { act, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router";

import { Submit } from "./Submit";
import { actionStorage, rememberAction } from "../api/requestKey";
import { PRINCIPAL, mockFetch, renderWith } from "../test/utils";

const GRANTS = [{
  connector_id: "mock-payments", provider_account_id: "mock-payments-account",
  action_type: "refund_payment", resource_id: "ch_north_1001", contract_version: "v1",
}];

function app() {
  return (
    <Routes>
      <Route path="/submit" element={<Submit />} />
      <Route path="/actions/:id" element={<p>tracking</p>} />
    </Routes>
  );
}

async function fill() {
  await userEvent.selectOptions(await screen.findByLabelText("Action"), "refund_payment");
  await userEvent.selectOptions(screen.getByLabelText("Charge"),
    screen.getByRole("option", { name: "Charge #1001 · Payments (test)" }));
  await userEvent.type(screen.getByLabelText(/Amount/), "15.00");
}

describe("submit form", () => {
  beforeEach(() => sessionStorage.clear());

  it("validates before sending and names the problem", async () => {
    const calls = mockFetch((url) => url === "/grants"
      ? { status: 200, body: GRANTS } : { status: 200, body: PRINCIPAL });
    renderWith(app(), { route: "/submit" });
    await userEvent.click(await screen.findByRole("button", { name: "Submit" }));
    expect(screen.getByText("Choose an action.")).toBeInTheDocument();
    expect(screen.getByLabelText("Action")).toHaveAttribute("aria-invalid", "true");
    expect(calls.some((c) => c.url === "/actions")).toBe(false);
  });

  it("keeps one request key across a failed attempt and a retry", async () => {
    let posts = 0;
    const calls = mockFetch((url) => {
      if (url === "/grants") return { status: 200, body: GRANTS };
      if (url === "/actions") {
        posts += 1;
        return posts === 1
          ? { status: 503, body: { error: { code: "unavailable", message: "Try again." } } }
          : { status: 201, body: { operation_id: "op-1", created: true } };
      }
      return { status: 200, body: PRINCIPAL };
    });
    renderWith(app(), { route: "/submit" });
    await fill();
    await userEvent.click(screen.getByRole("button", { name: "Submit" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Try again");
    await userEvent.click(screen.getByRole("button", { name: "Retry the same request" }));
    expect(await screen.findByText("tracking")).toBeInTheDocument();

    const keys = calls.filter((c) => c.url === "/actions")
      .map((c) => JSON.parse(c.init.body as string).request_key);
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
    expect(JSON.parse(calls.find((c) => c.url === "/actions")!.init.body as string).payload)
      .toEqual({ charge_id: "ch_north_1001", amount_minor: 1500 });
    // The response alone never forgets the key: only the detail page
    // the person lands on does (see ActionDetail.test.tsx).
    expect(JSON.parse(sessionStorage.getItem(actionStorage(`${PRINCIPAL.tenant_id}.${PRINCIPAL.principal_id}`))!).request_key).toBe(keys[0]);
  });

  it("survives a refresh with the same key", async () => {
    mockFetch((url) => url === "/grants" ? { status: 200, body: GRANTS } : { status: 200, body: PRINCIPAL });
    const first = renderWith(app(), { route: "/submit" });
    const key = await screen.findByText(/^ui-/);
    const shown = key.textContent;
    first.unmount();
    renderWith(app(), { route: "/submit" });
    expect((await screen.findByText(/^ui-/)).textContent).toBe(shown);
  });

  it("retains the entire request across reload after a lost response", async () => {
    const bodies: unknown[] = [];
    mockFetch((url, init) => {
      if (url === "/grants") return { status: 200, body: GRANTS };
      if (url === "/actions") {
        bodies.push(JSON.parse(init.body as string));
        return { status: 503, body: { error: { code: "unavailable", message: "Try again." } } };
      }
      return { status: 200, body: PRINCIPAL };
    });
    const first = renderWith(app(), { route: "/submit" });
    await fill();
    await userEvent.click(screen.getByRole("button", { name: "Submit" }));
    await screen.findByRole("alert");
    first.unmount();
    renderWith(app(), { route: "/submit" });
    expect(await screen.findByText("$15.00")).toBeVisible();
    expect(screen.queryByLabelText("Amount (USD)")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Retry the same request" }));
    await screen.findByRole("alert");
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toEqual(bodies[0]);
  });

  it("does not send if retaining the request fails", async () => {
    const calls = mockFetch((url) => ({ status: 200, body: url === "/grants" ? GRANTS : PRINCIPAL }));
    renderWith(app(), { route: "/submit" });
    await fill();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("storage full"); });
    await userEvent.click(screen.getByRole("button", { name: "Submit" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No request was sent by this attempt");
    expect(calls.some((c) => c.url === "/actions")).toBe(false);
  });

  it("does not show another person's retained request", async () => {
    rememberAction("other-tenant.other-person", { request_key: "ui-other", action_type: "refund_payment", provider_account_id: "mock-payments-account", contract_version: "v1", payload: { charge_id: "ch_other", amount_minor: 99999 } });
    mockFetch((url) => ({ status: 200, body: url === "/grants" ? GRANTS : PRINCIPAL }));
    renderWith(app(), { route: "/submit" });
    expect(await screen.findByLabelText("Action")).toHaveValue("");
    expect(screen.queryByText("Request retained in this browser")).toBeNull();
  });

  it("blocks a damaged retained request instead of silently replacing it", async () => {
    const owner = `${PRINCIPAL.tenant_id}.${PRINCIPAL.principal_id}`;
    sessionStorage.setItem(actionStorage(owner), "broken JSON");
    const calls = mockFetch((url) => ({ status: 200, body: url === "/grants" ? GRANTS : PRINCIPAL }));
    renderWith(app(), { route: "/submit" });
    expect(await screen.findByRole("alert")).toHaveTextContent("No request was sent by this attempt");
    expect(screen.getByRole("button", { name: "Submit" })).toBeDisabled();
    expect(sessionStorage.getItem(actionStorage(owner))).toBe("broken JSON");
    expect(calls.some((c) => c.url === "/actions")).toBe(false);
  });

  it("refuses amounts that are not exact dollars and cents", async () => {
    const calls = mockFetch((url) => ({ status: 200, body: url === "/grants" ? GRANTS : PRINCIPAL }));
    renderWith(app(), { route: "/submit" });
    await userEvent.selectOptions(await screen.findByLabelText("Action"), "refund_payment");
    const amount = screen.getByLabelText(/Amount/);
    await userEvent.type(amount, "15.505");
    await userEvent.click(screen.getByRole("button", { name: "Submit" }));
    expect(screen.getByText(/at most two decimals/)).toBeVisible();
    expect(amount).toHaveAttribute("aria-invalid", "true");
    await userEvent.clear(amount);
    await userEvent.type(amount, "0.29");
    expect(screen.getByText("Refunds $0.29")).toBeVisible();
    expect(calls.some((c) => c.url === "/actions")).toBe(false);
  });

  it("requires an explicit resource rather than treating the placeholder as the first grant", async () => {
    const calls = mockFetch((url) => ({ status: 200, body: url === "/grants" ? GRANTS : PRINCIPAL }));
    renderWith(app(), { route: "/submit" });
    await userEvent.selectOptions(await screen.findByLabelText("Action"), "refund_payment");
    await userEvent.type(screen.getByLabelText(/Amount/), "500");
    await userEvent.click(screen.getByRole("button", { name: "Submit" }));
    expect(screen.getByText("Choose a resource you are allowed to act on.")).toBeVisible();
    expect(calls.some((c) => c.url === "/actions")).toBe(false);
  });

  it("keeps the selected charge when a grant refresh adds an earlier resource", async () => {
    const calls = mockFetch((url) => {
      if (url === "/grants") return { status: 200, body: GRANTS };
      if (url === "/actions") return { status: 201, body: { operation_id: "op-1", created: true } };
      return { status: 200, body: PRINCIPAL };
    });
    const { client } = renderWith(app(), { route: "/submit" });
    await fill();
    act(() => client.setQueryData(["grants"], [{ ...GRANTS[0], resource_id: "ch_other" }, ...GRANTS]));
    expect(await screen.findByRole("option", { name: "ch_other · Payments (test)" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Submit" }));
    expect(await screen.findByText("tracking")).toBeVisible();
    const body = JSON.parse(calls.find((c) => c.url === "/actions")!.init.body as string);
    expect(body.payload.charge_id).toBe("ch_north_1001");
  });

  it("offers a Stripe test-mode refund only through its own grant and form", async () => {
    const stripe = {
      connector_id: "stripe-test", provider_account_id: "stripe-test-account",
      action_type: "refund_payment", resource_id: "pi_demo123456", contract_version: "v1-demo",
    };
    const calls = mockFetch((url) => {
      if (url === "/grants") return { status: 200, body: [...GRANTS, stripe] };
      if (url === "/actions") return { status: 201, body: { operation_id: "op-1", created: true } };
      return { status: 200, body: PRINCIPAL };
    });
    renderWith(app(), { route: "/submit" });
    await userEvent.selectOptions(await screen.findByLabelText("Action"), "refund_stripe_test_payment");
    const intents = screen.getByLabelText("Stripe test payment");
    // The mock charge is not offered as a Stripe PaymentIntent.
    expect(screen.queryByRole("option", { name: /Charge #1001/ })).toBeNull();
    await userEvent.selectOptions(intents,
      screen.getByRole("option", { name: "Stripe test payment ···3456 · Stripe (test mode)" }));
    await userEvent.type(screen.getByLabelText(/Amount/), "7");
    await userEvent.click(screen.getByRole("button", { name: "Submit" }));
    expect(await screen.findByText("tracking")).toBeVisible();
    const body = JSON.parse(calls.find((c) => c.url === "/actions")!.init.body as string);
    expect(body).toMatchObject({
      action_type: "refund_stripe_test_payment", provider_account_id: "stripe-test-account",
      contract_version: "v1-demo", payload: { payment_intent: "pi_demo123456", amount_minor: 700 },
    });
  });
});
