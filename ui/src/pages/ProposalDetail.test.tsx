import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router";

import { ProposalDetail } from "./ProposalDetail";
import { PRINCIPAL, mockFetch, renderWith } from "../test/utils";

const REVIEWER = { ...PRINCIPAL, principal_id: "rev-1", roles: ["reviewer"] };
const OPERATION = { operation_id: "op-1", action_type: "send_refund_confirmation", resource_id: "cust_north_3002", payload: {}, requested_by: "requester-1", attempts: [] };
const PROPOSAL = {
  proposal_id: "pr-1", operation_id: "op-1", action: "ATTEST_APPLIED",
  rationale: "Seen in the dashboard.", attested_reference: "re_123",
  proposed_by: "inv-1", created_at: "2026-10-19T10:00:00Z", expires_at: "2026-10-19T10:15:00Z",
  status: "open",
  binding: { seq: 4, state: "ESCALATED_UNRESOLVED", payload_sha256: "a".repeat(64),
             evidence_digest: "b".repeat(64), connector_id: "mock-email", contract_version: "v1" },
  binding_digest: "c".repeat(64),
  current: { seq: 4, state: "ESCALATED_UNRESOLVED", evidence_digest: "b".repeat(64), eligibility: null },
  decision: null,
};

function app() {
  return <Routes><Route path="/proposals/:proposalId" element={<ProposalDetail />} /></Routes>;
}

describe("proposal review", () => {
  it("approves with the digest shown and explains a stale refusal", async () => {
    const decisions: Record<string, unknown>[] = [];
    mockFetch((url, init) => {
      if (url === "/auth/me") return { status: 200, body: REVIEWER };
      if (url === "/proposals/pr-1/decision") {
        decisions.push(JSON.parse(init.body as string));
        return { status: 409, body: { error: {
          code: "stale_approval", message: "Changed.", reason: "evidence_changed", proposal: PROPOSAL,
        } } };
      }
      if (url === "/proposals/pr-1") return { status: 200, body: PROPOSAL };
      if (url === "/actions/op-1") return { status: 200, body: OPERATION };
      return { status: 404, body: { error: { code: "not_found", message: "Not found." } } };
    });
    renderWith(app(), { route: "/proposals/pr-1" });
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/new evidence was recorded/)).toBeInTheDocument();
    expect(decisions[0]).toMatchObject({ decision: "approve", binding_digest: "c".repeat(64) });
  });

  it("reuses the decision key when a response is lost", async () => {
    const keys: string[] = [];
    let attempt = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
      const json = (status: number, body: unknown) =>
        new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
      if (url === "/auth/me") return json(200, REVIEWER);
      if (url === "/proposals/pr-1") return json(200, PROPOSAL);
      if (url === "/actions/op-1") return json(200, OPERATION);
      if (url === "/proposals/pr-1/decision") {
        keys.push(JSON.parse(init.body as string).decision_key);
        attempt += 1;
        if (attempt === 1) throw new TypeError("network down");
        return json(200, { outcome: "replayed", reason: null, proposal: { ...PROPOSAL, status: "approved" } });
      }
      return json(404, { error: { code: "not_found", message: "x" } });
    }));
    renderWith(app(), { route: "/proposals/pr-1" });
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/could not be reached/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/Already decided by you/)).toBeInTheDocument();
    expect(keys[0]).toBe(keys[1]);
  });

  it("offers no decision to the proposer or a non-reviewer", async () => {
    mockFetch((url) => {
      if (url === "/auth/me") return { status: 200, body: { ...REVIEWER, principal_id: "inv-1", roles: ["investigator", "reviewer"] } };
      if (url === "/proposals/pr-1") return { status: 200, body: PROPOSAL };
      return { status: 404, body: { error: { code: "not_found", message: "x" } } };
    });
    renderWith(app(), { route: "/proposals/pr-1" });
    expect(await screen.findByText("Seen in the dashboard.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });

  it("compares what was seen with now and explains a stale proposal", async () => {
    mockFetch((url) => {
      if (url === "/auth/me") return { status: 200, body: REVIEWER };
      if (url === "/proposals/pr-1") return { status: 200, body: { ...PROPOSAL, status: "stale",
        current: { ...PROPOSAL.current, seq: 6, evidence_digest: "d".repeat(64) } } };
      return { status: 404, body: { error: { code: "not_found", message: "x" } } };
    });
    renderWith(app(), { route: "/proposals/pr-1" });
    expect(await screen.findByText(/This proposal is stale/)).toBeInTheDocument();
    expect(screen.getByText(/new evidence was recorded/)).toBeInTheDocument();
    expect(screen.getAllByText("Changed")).toHaveLength(1);
    expect(screen.getByText("New evidence recorded since")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "What you saw (when proposed)" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });

  it("does not flag an approved proposal's own result as a change", async () => {
    mockFetch((url) => {
      if (url === "/auth/me") return { status: 200, body: REVIEWER };
      if (url === "/proposals/pr-1") return { status: 200, body: { ...PROPOSAL, status: "approved",
        current: { ...PROPOSAL.current, seq: 6, state: "SUCCEEDED", evidence_digest: "d".repeat(64) } } };
      if (url === "/actions/op-1") return { status: 200, body: OPERATION };
      return { status: 404, body: { error: { code: "not_found", message: "x" } } };
    });
    renderWith(app(), { route: "/proposals/pr-1" });
    expect(await screen.findByText("Approved at the bound version (v4). Succeeded is the result of this approval.")).toBeInTheDocument();
    expect(screen.queryByText("Changed")).toBeNull();
    expect(screen.queryByText("New evidence recorded since")).toBeNull();
    expect(screen.queryByText(/Nothing has changed since/)).toBeNull();
  });

  it("says nothing changed while an open proposal still matches the record", async () => {
    mockFetch((url) => {
      if (url === "/auth/me") return { status: 200, body: REVIEWER };
      if (url === "/proposals/pr-1") return { status: 200, body: PROPOSAL };
      if (url === "/actions/op-1") return { status: 200, body: OPERATION };
      return { status: 404, body: { error: { code: "not_found", message: "x" } } };
    });
    renderWith(app(), { route: "/proposals/pr-1" });
    expect(await screen.findByText("Nothing has changed since the proposal was made.")).toBeInTheDocument();
    expect(screen.getByText("No new evidence")).toBeInTheDocument();
    expect(screen.queryByText("Changed")).toBeNull();
  });

  it("offers no decision to the original requester even with the reviewer role", async () => {
    mockFetch((url) => {
      if (url === "/auth/me") return { status: 200, body: REVIEWER };
      if (url === "/proposals/pr-1") return { status: 200, body: PROPOSAL };
      if (url === "/actions/op-1") return { status: 200, body: { ...OPERATION, requested_by: "rev-1" } };
      return { status: 404, body: { error: { code: "not_found", message: "x" } } };
    });
    renderWith(app(), { route: "/proposals/pr-1" });
    expect(await screen.findByText(/You requested this action/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });

  it("withholds decisions when the action evidence cannot be loaded", async () => {
    mockFetch((url) => url === "/auth/me" ? { status: 200, body: REVIEWER }
      : url === "/proposals/pr-1" ? { status: 200, body: PROPOSAL }
      : { status: 503, body: { error: { code: "unavailable", message: "Unavailable." } } });
    renderWith(app(), { route: "/proposals/pr-1" });
    expect(await screen.findByRole("button", { name: "Retry loading evidence" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });
});
