import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router";

import { Proposals } from "./Proposals";
import { PRINCIPAL, mockFetch, renderWith } from "../test/utils";

const PROPOSAL = {
  proposal_id: "pr-1", operation_id: "op-1", action: "ATTEST_APPLIED", rationale: "Seen.",
  attested_reference: "msg_1", proposed_by: "5afe0000-0000-4000-8000-000000000102",
  created_at: "2026-10-19T10:00:00Z", expires_at: "2026-10-19T10:15:00Z", status: "open",
};

function show(items: unknown[]) {
  mockFetch((url) => {
    if (url === "/auth/me") return { status: 200, body: { ...PRINCIPAL, roles: ["reviewer"] } };
    if (url.startsWith("/proposals?status=open")) return { status: 200, body: { items } };
    if (url.startsWith("/proposals?")) return { status: 200, body: { items: [] } };
    if (url === "/actions/op-1") {
      return { status: 200, body: { operation_id: "op-1", action_type: "send_refund_confirmation",
        resource_id: "cust_north_3002", payload: {} } };
    }
    return { status: 404, body: { error: { code: "not_found", message: "x" } } };
  });
  renderWith(<Routes><Route path="/proposals" element={<Proposals />} /></Routes>, { route: "/proposals" });
}

describe("reviews list", () => {
  it("titles each review by what it settles and names the proposer", async () => {
    show([PROPOSAL]);
    const link = await screen.findByRole("link", { name: "Confirm the refund email was delivered" });
    expect(link).toHaveAttribute("href", "/proposals/pr-1");
    expect(screen.getByText("Refund email to Customer #3002")).toBeInTheDocument();
    expect(screen.getByText("Sam Chen")).toBeInTheDocument();
    expect(screen.getByText("Person")).toBeInTheDocument();
    expect(screen.queryByText(/op-1/)).toBeNull();
  });

  it("explains an empty list", async () => {
    show([]);
    expect(await screen.findByText("Nothing waiting for review.")).toBeInTheDocument();
    expect(screen.getByText(/can need review before anyone has proposed one/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Actions that need review" })).toHaveAttribute("href", "/actions?group=needs_review");
    expect(screen.getByRole("link", { name: "Approved reviews" })).toHaveAttribute("href", "/proposals?status=approved");
    await userEvent.click(screen.getByRole("radio", { name: "Approved" }));
    expect(await screen.findByText("No approved reviews yet.")).toBeInTheDocument();
  });
});
