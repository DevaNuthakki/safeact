import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router";

import { WorkflowDetail } from "./WorkflowDetail";
import { WorkflowSubmit } from "./WorkflowSubmit";
import { pendingWorkflow, rememberWorkflow } from "../api/workflowRequest";
import { PRINCIPAL, mockFetch, renderWith } from "../test/utils";

const OWNER = `${PRINCIPAL.tenant_id}.${PRINCIPAL.principal_id}`;
const GRANTS = [
  { connector_id: "mock-payments", action_type: "charge_shipping_fee", resource_id: "cust_north_3001",
    provider_account_id: "payments-exact", contract_version: "v1" },
  { connector_id: "mock-crm", action_type: "create_replacement_order", resource_id: "case_north_2001",
    provider_account_id: "crm-exact", contract_version: "v1" },
];
const REQUEST = { request_key: "ui-retained-key", customer_id: "cust_north_3001",
  case_id: "case_north_2001", sku: "sku-one", fee_minor: 500,
  payments_account_id: "payments-exact", crm_account_id: "crm-exact",
  charge_contract: "v1", order_contract: "v1", compensation_contract: "v1" };
const WORKFLOW = { workflow_id: "wf-1", request_key: REQUEST.request_key, status: "UNRESOLVED",
  created_at: "2026-10-22T10:00:00Z", plan: { order: { resource_id: "case_north_2001" } },
  steps: [{ step: "charge", operation_id: "charge-1", state: "SUCCEEDED", compensates_operation_id: null as string | null },
    { step: "order", operation_id: "order-1", state: "ESCALATED_UNRESOLVED", compensates_operation_id: null }] };

function form() {
  return <Routes>
    <Route path="/workflows/new" element={<WorkflowSubmit />} />
    <Route path="/workflows/:workflowId" element={<p>Tracking existing workflow</p>} />
  </Routes>;
}
async function fill() {
  await userEvent.selectOptions(await screen.findByLabelText("Customer to charge"),
    screen.getByRole("option", { name: "Customer #3001 · Payments (test)" }));
  await userEvent.selectOptions(screen.getByLabelText("Support case"),
    screen.getByRole("option", { name: "Support case #2001 · CRM (test)" }));
  await userEvent.type(screen.getByLabelText("Replacement SKU"), "sku-one");
  await userEvent.type(screen.getByLabelText("Shipping fee (USD)"), "5.00");
}
function detail(workflow = WORKFLOW, operationError = false) {
  mockFetch((url) => {
    if (url === "/auth/me") return { status: 200, body: PRINCIPAL };
    if (url === "/workflows/wf-1") return { status: 200, body: workflow };
    if (operationError) return { status: 503, body: {} };
    const step = workflow.steps.find((s) => `/actions/${s.operation_id}` === url)!;
    return { status: 200, body: { operation_id: step.operation_id, action_type: step.step, payload: {},
      workflow: { step: step.step }, created_at: workflow.created_at, transitions: [],
      success_provenance: "human_attested", attempts: [{ attempt_id: step.operation_id,
        ordinal: 1, outcomes: [{ evidence_kind: "applied", evidence_reference: "<img src=x onerror=alert(1)>",
          provenance: "human_attested", created_at: workflow.created_at }] }] } };
  });
  return renderWith(<Routes><Route path="/workflows/:workflowId" element={<WorkflowDetail />} /></Routes>,
    { route: "/workflows/wf-1" });
}

describe("replacement workflow", () => {
  beforeEach(() => sessionStorage.clear());

  it("uses exact granted accounts and retains the whole intent before dispatch", async () => {
    const calls = mockFetch((url) => {
      if (url === "/auth/me") return { status: 200, body: PRINCIPAL };
      if (url === "/grants") return { status: 200, body: GRANTS };
      expect(pendingWorkflow(OWNER)?.sku).toBe("sku-one");
      return { status: 201, body: { workflow_id: "wf-1", created: true } };
    });
    renderWith(form(), { route: "/workflows/new" });
    await fill();
    await userEvent.click(screen.getByRole("button", { name: "Start replacement workflow" }));
    expect(await screen.findByText("Tracking existing workflow")).toBeVisible();
    const body = JSON.parse(calls.find((c) => c.url === "/workflows")!.init.body as string);
    expect(body).toMatchObject({ ...REQUEST, request_key: expect.stringMatching(/^ui-/),
      charge_contract: "v1", order_contract: "v1", compensation_contract: "v1" });
    expect(pendingWorkflow(OWNER)).toEqual(body); // response alone does not erase it
  });

  it("reloads after a lost reply and replays exactly the same request", async () => {
    let posts = 0;
    const calls = mockFetch((url) => {
      if (url === "/auth/me") return { status: 200, body: PRINCIPAL };
      if (url === "/grants") return { status: 200, body: GRANTS };
      posts += 1;
      return posts === 1 ? { status: 503, body: {} }
        : { status: 200, body: { workflow_id: "wf-1", created: false } };
    });
    const first = renderWith(form(), { route: "/workflows/new" });
    await fill();
    await userEvent.click(screen.getByRole("button", { name: "Start replacement workflow" }));
    expect(await screen.findByRole("alert")).toBeVisible();
    first.unmount();
    renderWith(form(), { route: "/workflows/new" });
    await userEvent.click(await screen.findByRole("button", { name: "Retry the same request" }));
    expect(await screen.findByText("Tracking existing workflow")).toBeVisible();
    const bodies = calls.filter((c) => c.url === "/workflows").map((c) => c.init.body);
    expect(bodies).toHaveLength(2);
    expect(bodies[0]).toBe(bodies[1]);
  });

  it("preserves selected resources when refreshed grants insert earlier choices", async () => {
    const calls = mockFetch((url) => {
      if (url === "/auth/me") return { status: 200, body: PRINCIPAL };
      if (url === "/grants") return { status: 200, body: GRANTS };
      return { status: 201, body: { workflow_id: "wf-1", created: true } };
    });
    const { client } = renderWith(form(), { route: "/workflows/new" });
    await fill();
    act(() => client.setQueryData(["grants"], [
      { ...GRANTS[0], resource_id: "cust_other" },
      { ...GRANTS[1], resource_id: "case_other" }, ...GRANTS,
    ]));
    expect(await screen.findByRole("option", { name: "cust_other · Payments (test)" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Start replacement workflow" }));
    expect(await screen.findByText("Tracking existing workflow")).toBeVisible();
    const body = JSON.parse(calls.find((c) => c.url === "/workflows")!.init.body as string);
    expect(body.customer_id).toBe("cust_north_3001");
    expect(body.case_id).toBe("case_north_2001");
  });

  it("does not display another principal's pending intent", async () => {
    rememberWorkflow("other-tenant.other-person", REQUEST);
    mockFetch((url) => ({ status: 200, body: url === "/grants" ? GRANTS : PRINCIPAL }));
    renderWith(form(), { route: "/workflows/new" });
    expect(await screen.findByLabelText("Customer to charge")).toHaveValue("");
    expect(screen.queryByText("Submission retained.")).toBeNull();
  });

  it("validates before creating a workflow", async () => {
    const calls = mockFetch((url) => ({ status: 200, body: url === "/grants" ? GRANTS : PRINCIPAL }));
    renderWith(form(), { route: "/workflows/new" });
    await userEvent.click(await screen.findByRole("button", { name: "Start replacement workflow" }));
    expect(screen.getByText("Choose a customer to charge.")).toBeVisible();
    expect(screen.getByText("Choose a support case.")).toBeVisible();
    expect(screen.getByLabelText("Replacement SKU")).toHaveAttribute("aria-invalid", "true");
    expect(calls.some((c) => c.url === "/workflows")).toBe(false);
  });

  it("blocks submission if retaining the request fails", async () => {
    const calls = mockFetch((url) => ({ status: 200, body: url === "/grants" ? GRANTS : PRINCIPAL }));
    renderWith(form(), { route: "/workflows/new" });
    await fill();
    const storage = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("full"); });
    await userEvent.click(screen.getByRole("button", { name: "Start replacement workflow" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No request was sent");
    expect(calls.some((c) => c.url === "/workflows")).toBe(false);
    storage.mockRestore();
  });

  it("explains withheld compensation for an uncertain order", async () => {
    detail();
    expect(await screen.findByRole("heading", { name: "Replacement needs investigation" })).toBeVisible();
    expect(screen.getByText(/a refund is withheld/)).toBeVisible();
    expect(screen.getByRole("link", { name: "Inspect order evidence" })).toHaveAttribute("href", "/actions/order-1");
    expect(within(screen.getByRole("region", { name: "Refund if the order fails" }))
      .getByText(/Created only after/)).toBeVisible();
  });

  it("keeps refunded work distinct from a completed replacement and marks attestation", async () => {
    const { container } = detail({ ...WORKFLOW, status: "COMPENSATED_VERIFIED", steps: [
      WORKFLOW.steps[0]!, { ...WORKFLOW.steps[1]!, state: "FAILED_CONFIRMED" },
      { step: "compensation", operation_id: "refund-1", state: "SUCCEEDED", compensates_operation_id: "charge-1" },
    ] });
    expect(await screen.findByRole("heading", { name: "Refund confirmed" })).toBeVisible();
    expect(screen.getByText(/replacement was not completed/)).toBeVisible();
    expect(await screen.findAllByText(/Success attested by a reviewer/)).toHaveLength(3);
    expect(screen.getByRole("link", { name: "original shipping charge" })).toHaveAttribute("href", "/actions/charge-1");
    expect(container.querySelector("img")).toBeNull();
  });

  it("does not substitute missing evidence for a failed provider action", async () => {
    detail(WORKFLOW, true);
    expect(await screen.findAllByText(/Evidence could not be loaded/)).toHaveLength(2);
    expect(screen.getByText(/timeline is incomplete/)).toBeVisible();
  });

  it("clears only the matching pending intent after showing the result", async () => {
    rememberWorkflow(OWNER, REQUEST);
    const first = detail();
    await screen.findByRole("heading", { name: "Replacement for Support case #2001" });
    expect(pendingWorkflow(OWNER)).toBeNull();
    first.unmount();
    rememberWorkflow(OWNER, { ...REQUEST, request_key: "ui-newer-intent" });
    detail();
    await screen.findByRole("heading", { name: "Replacement for Support case #2001" });
    expect(pendingWorkflow(OWNER)?.request_key).toBe("ui-newer-intent");
  });
});
