import {
  actionLabel, centsToDollars, connectorLabel, dollarsToCents, evidenceSummary, humanize, latestRefundStatus, money,
  operationName, proposalTitle, refundStatusLabel, reportedRefundStatus, resourceLabel,
} from "./labels";
import { ago } from "./time";

describe("labels", () => {
  it("names known actions and humanizes the rest", () => {
    expect(actionLabel("refund_payment")).toBe("Refund payment");
    expect(actionLabel("create_replacement_order")).toBe("Create replacement order");
    expect(actionLabel("archive_old_ticket")).toBe("Archive old ticket");
    expect(humanize("may_have_dispatched")).toBe("May have dispatched");
  });

  it("says how long ago, coarsely", () => {
    const now = Date.parse("2026-10-25T12:00:00Z");
    expect(ago(new Date("2026-10-25T11:59:50Z"), now)).toBe("just now");
    expect(ago(new Date("2026-10-25T11:56:00Z"), now)).toBe("4 min ago");
    expect(ago(new Date("2026-10-25T09:00:00Z"), now)).toBe("3 h ago");
    expect(ago(new Date("2026-10-23T12:00:00Z"), now)).toBe("2 d ago");
    expect(ago(new Date("2026-10-25T12:10:00Z"), now)).toBe("in 10 min");
  });
});

describe("friendly names", () => {
  it("names resources, connectors and operations", () => {
    expect(resourceLabel("ch_north_1002")).toBe("Charge #1002");
    expect(resourceLabel("case_south_2004")).toBe("Support case #2004");
    expect(resourceLabel("cust_north_3001")).toBe("Customer #3001");
    expect(resourceLabel("pi_3UUbA2DnpP0MDY8U0R6eGrx5")).toBe("Stripe test payment ···Grx5");
    expect(resourceLabel("ticket-9")).toBe("ticket-9");
    expect(connectorLabel("mock-crm")).toBe("CRM (test)");
    expect(connectorLabel("other")).toBe("other");
    expect(operationName({ action_type: "refund_payment", resource_id: "ch_north_1002",
      payload: { charge_id: "ch_north_1002", amount_minor: 1500 } })).toBe("Refund $15.00 on Charge #1002");
    expect(operationName({ action_type: "send_refund_confirmation", resource_id: "cust_north_3002" }))
      .toBe("Refund email to Customer #3002");
  });

  it("titles each review by what it would settle", () => {
    const order = { action_type: "create_replacement_order", resource_id: "case_north_2002" };
    const email = { action_type: "send_refund_confirmation", resource_id: "cust_north_3002" };
    const refund = { action_type: "refund_payment", resource_id: "ch_north_1002" };
    expect(proposalTitle("ATTEST_APPLIED", email)).toBe("Confirm the refund email was delivered");
    expect(proposalTitle("ATTEST_APPLIED", order)).toBe("Confirm replacement order #2002 was created");
    expect(proposalTitle("REVERIFY", refund)).toBe("Check again whether the refund on charge #1002 went through");
    expect(proposalTitle("PROTECTED_RETRY", order)).toBe("Retry replacement order #2002");
    expect(proposalTitle("ATTEST_APPLIED")).toBeNull();
  });
});

describe("money", () => {
  it("formats integer cents as dollars", () => {
    expect(money(1500)).toBe("$15.00");
    expect(money(29)).toBe("$0.29");
    expect(money(1)).toBe("$0.01");
    expect(money(10_000_000)).toBe("$100,000.00");
    expect(money(123_456_789)).toBe("$1,234,567.89");
    expect(money(1.5)).toBe("an invalid amount");
    expect(centsToDollars(1550)).toBe("15.50");
    expect(centsToDollars(10_000_000)).toBe("100000.00");
  });

  it.each([
    ["0.29", 29], ["15.5", 1550], ["15.50", 1550], ["15", 1500], ["0.01", 1],
    [" 7.1 ", 710], ["0.1", 10], ["100000.00", 10_000_000], ["100000", 10_000_000],
    ["1.13", 113], ["4.35", 435], ["0.07", 7],
  ])("parses %j exactly as %d cents", (text, cents) => {
    expect(dollarsToCents(text)).toBe(cents);
  });

  it.each([
    "15.505", "0", "0.00", "-1", "+1", "1e3", "1,000", "$5", " ", "", ".5", "5.", "1 000",
    "100000.01", "99999999999999999999", "9".repeat(400), "NaN", "Infinity", "0x10", "١٢",
  ])("refuses %j", (text) => {
    expect(dollarsToCents(text)).toBeNull();
  });
});

describe("refund status reported by Stripe", () => {
  it.each([
    ["pending", "Pending"], ["requires_action", "Requires action"], ["succeeded", "Succeeded"],
    ["failed", "Failed"], ["canceled", "Canceled"], ["unreported", "Not reported"],
  ])("labels %s as %s", (status, label) => {
    expect(refundStatusLabel(status)).toBe(label);
  });

  it("shows an unrecognized status verbatim", () => {
    expect(refundStatusLabel("partially_reversed_2027")).toBe("partially_reversed_2027");
  });

  it("reads only a string at details.provider.refund_status", () => {
    expect(reportedRefundStatus({ provider: { refund_status: "pending" } })).toBe("pending");
    expect(reportedRefundStatus({ provider: { refund_status: 3 } })).toBeNull();
    expect(reportedRefundStatus({ provider: "pending" })).toBeNull();
    expect(reportedRefundStatus({ refund_status: "pending" })).toBeNull();
    expect(reportedRefundStatus({})).toBeNull();
  });

  it("takes the latest recorded status across attempts", () => {
    const row = (details: Record<string, unknown>) => ({ details });
    expect(latestRefundStatus([
      { outcomes: [row({ provider: { refund_status: "pending" } }), row({})] },
      { outcomes: [row({ provider: { refund_status: "succeeded" } })] },
    ])).toBe("succeeded");
    expect(latestRefundStatus([{ outcomes: [row({ note: "mock" })] }])).toBeNull();
  });
});

describe("evidence summary", () => {
  const t = (seq: number, to_state: string) => ({ seq, to_state });
  const row = (observed_seq: number, outcome: string, evidence_kind: string | null, minute = 0, provenance = "system") => ({
    outcome, evidence_kind, observed_seq, provenance, created_at: `2026-10-26T18:${String(minute).padStart(2, "0")}:00Z`,
  });

  it("summarises an escalated order: the send's answer, then lookups before and after escalation", () => {
    // Shape of the demo order: dispatch at v2, six lookups while verifying, escalation at v15, sixty after.
    const transitions = [t(1, "CLAIMED"), t(2, "DISPATCHING"), t(3, "UNKNOWN"),
      ...[4, 6, 8, 10, 12, 14].map((n) => t(n, "VERIFYING")), ...[5, 7, 9, 11, 13].map((n) => t(n, "UNKNOWN")),
      t(15, "ESCALATED_UNRESOLVED")];
    const outcomes = [row(2, "may_have_dispatched", null),
      ...[2, 4, 6, 8, 10, 12].map((n, i) => row(n === 2 ? 4 : n, "may_have_dispatched", "not_found", i)),
      ...Array.from({ length: 60 }, (_, i) => row(15, "may_have_dispatched", "not_found", 10 + (i % 40)))];
    const summary = evidenceSummary({ action_type: "create_replacement_order", transitions, attempts: [{ outcomes }] });
    expect(summary.text).toBe("1 send with no answer · 66 lookups, none found the order");
    expect(summary.split).toBe("6 while investigating, 60 after escalation");
    expect(summary.latest).toBe("2026-10-26T18:49:00Z");
    expect(summary.total).toBe(67);
  });

  it("summarises a refund verified after a lost response", () => {
    const summary = evidenceSummary({ action_type: "refund_payment",
      transitions: [t(2, "DISPATCHING"), t(3, "UNKNOWN"), t(4, "VERIFYING"), t(5, "SUCCEEDED")],
      attempts: [{ outcomes: [row(2, "may_have_dispatched", null), row(4, "confirmed_applied", "applied", 1)] }] });
    expect(summary.text).toBe("1 send with no answer · 1 lookup, found the refund applied");
    expect(summary.split).toBeNull();
  });

  it("names a reviewer attestation separately from system observations", () => {
    const summary = evidenceSummary({ action_type: "send_refund_confirmation",
      transitions: [t(2, "DISPATCHING"), t(3, "UNKNOWN"), t(4, "VERIFYING"), t(5, "ESCALATED_UNRESOLVED"), t(6, "SUCCEEDED")],
      attempts: [{ outcomes: [row(2, "may_have_dispatched", null), row(4, "may_have_dispatched", "inconclusive", 1),
        row(5, "confirmed_applied", "applied", 30, "human_attested")] }] });
    expect(summary.text).toBe("1 send with no answer · 1 lookup, it was inconclusive · 1 reviewer attestation");
  });

  it("never calls a check on a connector without lookup a provider lookup", () => {
    const summary = evidenceSummary({ action_type: "send_refund_confirmation",
      transitions: [t(2, "DISPATCHING"), t(3, "UNKNOWN"), t(4, "VERIFYING"), t(5, "ESCALATED_UNRESOLVED"), t(6, "SUCCEEDED")],
      attempts: [{ outcomes: [row(2, "may_have_dispatched", null),
        { ...row(4, "may_have_dispatched", "inconclusive", 1), details: { note: "no_lookup" } },
        { ...row(5, "may_have_dispatched", "inconclusive", 2), details: { note: "no_lookup" } },
        row(5, "confirmed_applied", "applied", 30, "human_attested")] }] });
    expect(summary.text).toBe("1 send with no answer · 2 checks with no provider lookup available · 1 reviewer attestation");
    expect(summary.text).not.toMatch(/lookups?,/);
    expect(summary.split).toBeNull();
  });

  it("breaks down lookups that disagree instead of hiding one", () => {
    const summary = evidenceSummary({ action_type: "refund_payment",
      transitions: [t(2, "DISPATCHING"), t(4, "VERIFYING")],
      attempts: [{ outcomes: [row(4, "may_have_dispatched", "not_found"), row(4, "confirmed_applied", "applied", 1)] }] });
    expect(summary.text).toBe("2 lookups: 1 found nothing, 1 found it applied");
  });
});
