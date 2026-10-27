import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router";

import { Actions } from "./Actions";
import { PRINCIPAL, mockFetch, renderWith } from "../test/utils";

function op(id: string, state: string, updated: string) {
  return {
    operation_id: id, request_key: `k-${id}`, action_type: "refund_payment", resource_id: `ch_${id}`,
    connector_id: "mock-payments", contract_version: "v1", created_at: updated, requested_by: "p-1",
    seq: 1, state, unknown_reason: null, updated_at: updated,
  };
}

const BY_STATE: Record<string, ReturnType<typeof op>[]> = {
  SUCCEEDED: [op("s1", "SUCCEEDED", "2026-10-25T10:00:00Z"), op("s2", "SUCCEEDED", "2026-10-25T09:00:00Z")],
  UNKNOWN: [op("u1", "UNKNOWN", "2026-10-25T08:00:00Z")],
  VERIFYING: [op("v1", "VERIFYING", "2026-10-25T11:00:00Z")],
  ESCALATED_UNRESOLVED: [op("e1", "ESCALATED_UNRESOLVED", "2026-10-25T07:00:00Z")],
};

function show(route = "/actions") {
  const calls = mockFetch((url) => {
    if (url === "/auth/me") return { status: 200, body: PRINCIPAL };
    const query = new URL(url, "http://x").searchParams;
    const state = query.get("state");
    if (state === "RECORDED") {
      // A full page with more behind it: the count says 100+.
      return { status: 200, body: { items: Array.from({ length: 100 }, (_, i) =>
        op(`r${i}`, "RECORDED", "2026-10-24T00:00:00Z")), next_before: "2026-10-23T00:00:00Z", next_before_id: "r99" } };
    }
    const items = state ? BY_STATE[state] ?? [] : Object.values(BY_STATE).flat();
    return { status: 200, body: { items, next_before: null, next_before_id: null } };
  });
  renderWith(<Routes><Route path="/actions" element={<Actions />} /></Routes>, { route });
  return calls;
}

// Role queries by name are slow in jsdom on a full page: allow for a busy run.
const tile = (name: RegExp) => screen.findByRole("button", { name }, { timeout: 5_000 });

describe("actions list", () => {
  it("attributes an MCP agent without hiding the human requester rows", async () => {
    mockFetch((url) => url === "/auth/me" ? { status: 200, body: PRINCIPAL } : { status: 200, body: {
      items: [{ ...op("agent", "SUCCEEDED", "2026-10-25T10:00:00Z"), requested_by: "5afe0000-0000-4000-8000-000000000105" },
        { ...op("human", "SUCCEEDED", "2026-10-25T09:00:00Z"), requested_by: PRINCIPAL.principal_id }],
      next_before: null, next_before_id: null,
    } });
    renderWith(<Routes><Route path="/actions" element={<Actions />} /></Routes>, { route: "/actions" });
    expect(await screen.findByRole("columnheader", { name: "Requested by" })).toBeInTheDocument();
    expect(screen.getByText("AI agent · MCP")).toBeInTheDocument();
    expect(screen.getByText("Support assistant")).toBeInTheDocument();
    expect(screen.getByText("North Requester (you)")).toBeInTheDocument();
    expect(screen.getByText("Person")).toBeInTheDocument();
  });

  it("counts by group, exactly below 100 and 100+ beyond", async () => {
    show();
    expect(await tile(/^Succeeded\s*2\s*Safely completed$/)).toBeInTheDocument();
    expect(await tile(/^Checking\s*2 Asking the provider$/)).toBeInTheDocument();
    expect(await tile(/^Needs review\s*1 A person should look$/)).toBeInTheDocument();
    expect(await tile(/^In progress\s*100\+ Recorded or being processed$/)).toBeInTheDocument();
    expect(await tile(/^Failed or cancelled\s*0 Not done or stopped$/)).toBeInTheDocument();
  });

  it("names actions for people and links the whole row", async () => {
    show();
    const links = await screen.findAllByRole("link", { name: "Refund payment" });
    expect(links[0]).toHaveAttribute("href", "/actions/s1");
    expect(links[0]!.closest("tr")).toHaveClass("row-link");
  });

  it("filters by a group of states, merged newest first", async () => {
    show();
    await userEvent.click(await tile(/Checking/));
    expect(await tile(/Checking/)).toHaveAttribute("aria-pressed", "true");
    const rows = await screen.findAllByRole("row");
    const resources = rows.slice(1).map((r) => within(r).getByText(/^ch_/).textContent);
    expect(resources).toEqual(["ch_v1", "ch_u1"]);
  });

  it("uses the server's state filter for a one-state group", async () => {
    const calls = show();
    await userEvent.click(await tile(/Needs review/));
    expect(await screen.findByText("ch_e1")).toBeInTheDocument();
    expect(calls.some((c) => c.url.startsWith("/actions?limit=25&state=ESCALATED_UNRESOLVED"))).toBe(true);
  });

  it("offers to clear filters when nothing matches", async () => {
    show("/actions?group=failed");
    expect(await screen.findByText("No actions match these filters.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(await screen.findByText("ch_s1")).toBeInTheDocument();
  });
});
