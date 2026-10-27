import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { OperationDetail, Proposal } from "../api/types";
import { AppRoutes } from "../App";
import { PREP_STEPS } from "../components/WalkthroughLayout";
import { parseWalkthrough } from "../demo/walkthrough";
import { CASES, EMAIL, ORDER, REFUND, proposal } from "../test/storyFixtures";
import { PRINCIPAL, mockFetch, renderWith } from "../test/utils";

// jsdom has no streaming transport. Live invalidation is tested separately.
beforeEach(() => vi.stubGlobal("EventSource", class {
  onopen = null; onerror = null;
  addEventListener() {}
  close() {}
}));

const REVIEWER = { ...PRINCIPAL, principal_id: "reviewer", roles: ["reviewer"] };
const missing = { status: 404, body: { error: { code: "not_found", message: "Unavailable" } } };

function setup({ email = EMAIL, ep = proposal(), principal = REVIEWER, cases = CASES, refund = REFUND } = {}) {
  return mockFetch((url) => {
    if (url === "/auth/me") return { status: 200, body: principal };
    if (url === "/demo-cases.json") return { status: 200, body: cases };
    if (url === `/actions/${CASES.email.operation_id}`) return { status: 200, body: email };
    if (url === `/actions/${CASES.refund.operation_id}`) return { status: 200, body: refund };
    if (url === `/actions/${CASES.order.operation_id}`) return { status: 200, body: ORDER };
    if (url === `/proposals/${CASES.email.proposal_id}`) return { status: 200, body: ep };
    if (url === `/proposals/${CASES.order.proposal_id}`) return { status: 200, body: proposal(false) };
    return missing;
  });
}

describe("guided portfolio story", () => {
  it("rejects a case index with cross-linked captured refusals", () => {
    expect(parseWalkthrough(CASES)).toEqual(CASES);
    expect(() => parseWalkthrough({ ...CASES, agent_refusal: { ...CASES.agent_refusal, operation_id: CASES.order.operation_id } })).toThrow();
    expect(() => parseWalkthrough({ ...CASES, stale_refusal: { ...CASES.stale_refusal, proposal_id: CASES.email.proposal_id } })).toThrow();
    expect(() => parseWalkthrough({ ...CASES, email: { ...CASES.email, operation_id: "bad" } })).toThrow();
  });

  it("opens the introduction at the root without a sign-in or writes", async () => {
    const calls = mockFetch(() => ({ status: 401 }));
    renderWith(<AppRoutes />);
    expect(await screen.findByRole("heading", { name: "When a response is lost, did the action happen?" })).toBeVisible();
    expect(screen.getByRole("navigation", { name: "Demo chapters" }).querySelectorAll("a")).toHaveLength(5);
    expect(calls.every((call) => !call.init.method || call.init.method === "GET")).toBe(true);
  });

  it("shows the exact preparation steps when the cases are not prepared", async () => {
    setup({ cases: null as unknown as typeof CASES });
    renderWith(<AppRoutes />, { route: "/demo/refund" });
    expect(await screen.findByRole("heading", { name: "The demo cases are not available" })).toBeVisible();
    for (const step of PREP_STEPS) expect(screen.getByText(step)).toBeInTheDocument();
    expect(PREP_STEPS).toContain(".venv/bin/python ui/scripts/prepare_walkthrough.py --compose");
  });

  it("requires a reviewer without impersonating one", async () => {
    setup({ principal: { ...REVIEWER, roles: ["investigator"] } });
    renderWith(<AppRoutes />, { route: "/demo/decision" });
    expect(await screen.findByRole("heading", { name: "Continue with the reviewer account" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Approve attestation" })).toBeNull();
  });

  it("names the person who requested the refund, not the agent", async () => {
    // The demo directory's fixed id for Alex Rivera (deploy/demo/bootstrap.py).
    setup({ refund: { ...REFUND, requested_by: "5afe0000-0000-4000-8000-000000000101" } });
    renderWith(<AppRoutes />, { route: "/demo/refund" });
    const line = (await screen.findByText("Requested by")).closest("p")!;
    expect(line).toHaveTextContent(/^Requested by\s*Alex Rivera\s*Person$/);
    expect(line).not.toHaveTextContent(/agent|MCP/i);
  });

  it("states the refund in full dollars and cents, and the naive retry's total", async () => {
    setup();
    renderWith(<AppRoutes />, { route: "/demo/refund" });
    expect(await screen.findByRole("heading", { name: "One $2,450.00 refund. A lost response." })).toBeVisible();
    expect(screen.getByText("$4,900.00 total")).toBeVisible();
    expect(screen.getByText(/A blind retry could have refunded it twice/)).toBeVisible();
    expect(document.body.textContent).not.toMatch(/\$2,450(?!\.00)/);
  });

  it("shows the connected email, recorded refusal and no automatic writes", async () => {
    const calls = setup();
    renderWith(<AppRoutes />, { route: "/demo/agent" });
    expect(await screen.findByRole("heading", { name: "Captured MCP response" })).toBeVisible();
    expect(screen.getByText(/not a live event in the REST/)).toBeVisible();
    // The meaning leads; the protocol error code is the technical detail.
    expect(screen.getByText("Refused: agents cannot attest that an effect was applied.")).toBeVisible();
    expect(screen.getByText("invalid_arguments").closest("p")).toHaveTextContent("Technical detail: invalid_arguments");
    await userEvent.click(screen.getByRole("link", { name: "Next: Human decision" }));
    expect(await screen.findByRole("heading", { name: "The same email · Customer #3002" })).toBeVisible();
    expect(screen.getByText("msg_demo_delivered_1")).toBeVisible();
    expect(screen.getByText("409 stale_approval")).toBeVisible();
    const order = screen.getByRole("region", { name: "The order · Case #2002" });
    expect(within(order).getByRole("button", { name: "Approve attestation" })).toBeDisabled();
    expect(calls.filter((call) => call.init.method === "POST")).toHaveLength(0);
  });

  it("labels the agent's attested email as reviewer-attested, never provider-confirmed", async () => {
    setup({ email: { ...EMAIL, state: "SUCCEEDED", outcome_class: "confirmed", success_provenance: "human_attested" } });
    renderWith(<AppRoutes />, { route: "/demo/agent" });
    const heading = await screen.findByRole("heading", { name: "The agent cannot mark its own work as done." });
    expect(heading.parentElement!.parentElement!).toHaveTextContent("Succeeded · reviewer-attested");
    expect(document.body.textContent).not.toMatch(/provider confirmed|Confirmed by evidence|Evidence settles what happened/i);
  });

  it("does not claim a provider lookup when verification only recorded no_lookup", async () => {
    const ambiguous = EMAIL.transitions[0]!;
    setup({ email: { ...EMAIL,
      transitions: [...EMAIL.transitions, { ...ambiguous, seq: 4, from_state: "UNKNOWN", to_state: "VERIFYING", reason: "verify_with_provider" }],
      attempts: [{ ...EMAIL.attempts[0]!, outcomes: [{ outcome: "may_have_dispatched", evidence_kind: "inconclusive",
        evidence_reference: null, observed_by: "reconciler", observed_seq: 4, provenance: "system",
        details: { note: "no_lookup" }, created_at: EMAIL.created_at }] }],
    } });
    renderWith(<AppRoutes />, { route: "/demo/agent" });
    expect(await screen.findByText("No reliable provider lookup")).toBeVisible();
    expect(screen.getByText(/outcome could not be independently verified/)).toBeVisible();
    expect(screen.queryByText("SafeAct checked with the provider")).toBeNull();
  });

  it.each(["expired", "changed", "proposer", "requester"])("withholds email approval when %s", async (condition) => {
    const ep = proposal();
    const email = { ...EMAIL };
    if (condition === "expired") ep.expires_at = "2020-01-01T00:00:00Z";
    if (condition === "changed") ep.current.evidence_digest = "d".repeat(64);
    if (condition === "proposer") ep.proposed_by = REVIEWER.principal_id;
    if (condition === "requester") {
      // Keep the known agent requester, and test a reviewer with that identity.
      setup({ ep, principal: { ...REVIEWER, principal_id: email.requested_by! } });
    } else setup({ ep, email });
    renderWith(<AppRoutes />, { route: "/demo/decision" });
    const region = await screen.findByRole("region", { name: "The same email · Customer #3002" });
    expect(within(region).queryByRole("button", { name: "Approve attestation" })).toBeNull();
  });

  it.each([
    ["open and unchanged", {}, {}, "Unchanged"],
    ["approved", { status: "approved", current: { seq: 6, state: "SUCCEEDED", evidence_digest: "d".repeat(64), eligibility: null } },
      { state: "SUCCEEDED", outcome_class: "confirmed", success_provenance: "human_attested" },
      "Approved at the bound version (v4). Succeeded is the result of this approval."],
    ["stale", { status: "stale", current: { seq: 6, state: "ESCALATED_UNRESOLVED", evidence_digest: "d".repeat(64), eligibility: null } },
      {}, "Changed since proposal"],
  ] as const)("compares the email's evidence only while it can matter: %s", async (_name, change, emailChange, expected) => {
    setup({ ep: { ...proposal(), ...change } as Proposal, email: { ...EMAIL, ...emailChange } as OperationDetail });
    renderWith(<AppRoutes />, { route: "/demo/decision" });
    const region = await screen.findByRole("region", { name: "The same email · Customer #3002" });
    expect(within(region).getByRole("table")).toHaveTextContent(expected);
    if (expected !== "Changed since proposal") expect(within(region).queryByText("Changed since proposal")).toBeNull();
  });

  it("never shows a raw state code as the main text of the order comparison", async () => {
    setup();
    renderWith(<AppRoutes />, { route: "/demo/decision" });
    const order = await screen.findByRole("region", { name: "The order · Case #2002" });
    const cells = within(order).getAllByRole("cell").filter((cell) => cell.querySelector(".state-code"));
    expect(cells.length).toBeGreaterThan(0);
    for (const cell of cells) {
      const code = cell.querySelector(".state-code")!.textContent!.replace("Technical state: ", "");
      const main = cell.textContent!.replace(cell.querySelector(".state-code")!.textContent!, "");
      expect(main).not.toContain(code);
      expect(main.trim().length).toBeGreaterThan(0);
    }
  });

  it("does not offer decisions when a proposal belongs to a different operation", async () => {
    setup({ ep: { ...proposal(), operation_id: CASES.order.operation_id } });
    renderWith(<AppRoutes />, { route: "/demo/decision" });
    expect(await screen.findByText(/These proposals do not match/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Approve attestation" })).toBeNull();
  });

  it("shows an already-attested email honestly rather than offering another decision", async () => {
    setup({ email: { ...EMAIL, state: "SUCCEEDED", success_provenance: "human_attested" }, ep: { ...proposal(), status: "approved" } });
    renderWith(<AppRoutes />, { route: "/demo/decision" });
    expect(await screen.findByText(/Succeeded · reviewer-attested/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Approve attestation" }) === null
      || screen.getAllByRole("button", { name: "Approve attestation" }).every((button) => button.hasAttribute("disabled"))).toBe(true);
  });

  it("keeps the complete decision unchanged after a lost reply", async () => {
    const bodies: unknown[] = [];
    let ep: Proposal = proposal();
    let email: OperationDetail = EMAIL;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
      const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
      if (url === "/auth/me") return json(REVIEWER);
      if (url === "/demo-cases.json") return json(CASES);
      if (url === `/actions/${CASES.email.operation_id}`) return json(email);
      if (url === `/actions/${CASES.order.operation_id}`) return json(ORDER);
      if (url === `/proposals/${CASES.email.proposal_id}`) return json(ep);
      if (url === `/proposals/${CASES.order.proposal_id}`) return json(proposal(false));
      if (url.endsWith("/decision")) {
        bodies.push(JSON.parse(init.body as string));
        if (bodies.length === 1) throw new TypeError("reply lost");
        ep = { ...ep, status: "approved" };
        email = { ...EMAIL, state: "SUCCEEDED", success_provenance: "human_attested" };
        return json({ outcome: "replayed", reason: null, proposal: ep });
      }
      return new Response(null, { status: 404 });
    }));
    renderWith(<AppRoutes />, { route: "/demo/decision" });
    await userEvent.type(await screen.findByLabelText("Decision note (optional)"), "Reference checked");
    const region = screen.getByRole("region", { name: "The same email · Customer #3002" });
    await userEvent.click(within(region).getByRole("button", { name: "Approve attestation" }));
    expect(await screen.findByText(/The decision response was lost/)).toBeVisible();
    expect(screen.getByLabelText("Decision note (optional)")).toBeDisabled();
    expect(within(region).getByRole("button", { name: "Reject proposal" })).toBeDisabled();
    await userEvent.click(within(region).getByRole("button", { name: "Retry approval" }));
    expect(await screen.findByText(/Succeeded · reviewer-attested/)).toBeVisible();
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toEqual(bodies[0]);
    expect(bodies[0]).toMatchObject({ decision: "approve", binding_digest: "c".repeat(64), note: "Reference checked" });
  });
});
