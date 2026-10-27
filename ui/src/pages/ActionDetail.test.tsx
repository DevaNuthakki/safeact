import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router";

import { ActionDetail } from "./ActionDetail";
import { actionStorage, rememberAction } from "../api/requestKey";
import { PRINCIPAL, mockFetch, renderWith } from "../test/utils";

const BASE = {
  operation_id: "op-1", request_key: "ui-1", action_type: "refund_payment", resource_id: "ch_north_1002",
  provider_account_id: "mock-payments-account", connector_id: "mock-payments", contract_version: "v1",
  payload: { charge_id: "ch_north_1002", amount_minor: 1500 }, payload_sha256: "a".repeat(64),
  provider_key_protected: true, created_at: "2026-10-19T10:00:00Z", requested_by: "p-1",
  seq: 3, success_provenance: null, workflow: null, contract: null, cancellation_requests: [],
  transitions: [{ seq: 3, from_state: "DISPATCHING", to_state: "UNKNOWN", reason: "ambiguous_no_response",
                  actor: "worker", attempt_id: "a-1", evidence: {}, created_at: "2026-10-19T10:00:01Z" }],
  attempts: [{ attempt_id: "a-1", ordinal: 1, created_at: "2026-10-19T10:00:00Z", outcomes: [{
    outcome: "may_have_dispatched", evidence_kind: "not_found", evidence_reference: null,
    observed_by: "recoverer", observed_seq: 3, provenance: "system",
    details: { note: "<img src=x onerror=alert(1)>" }, created_at: "2026-10-19T10:00:02Z",
  }] }],
};

function show(detail: unknown, state?: unknown, search = "") {
  mockFetch((url) => url === "/auth/me" ? { status: 200, body: PRINCIPAL }
    : url === "/actions/op-1" ? { status: 200, body: detail }
    : { status: 404, body: { error: { code: "not_found", message: "x" } } });
  return renderWith(<Routes><Route path="/actions/:operationId" element={<ActionDetail />} /></Routes>,
                    { route: { pathname: "/actions/op-1", search, state } });
}

describe("action detail", () => {
  it("shows when the next provider lookup happens and the deadline", async () => {
    show({ ...BASE, state: "UNKNOWN", outcome_class: "unknown", next_check: {
      kind: "scheduled_lookup", not_before: "2026-10-19T10:00:05Z",
      deadline: "2026-10-19T10:30:00Z", basis: "the next paced provider lookup",
    } });
    // Once, in plain words, on the overview; the raw schedule under Technical details.
    expect(await screen.findByText(/^SafeAct will ask the provider again/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Technical details" }));
    expect(screen.getByText(/the next paced provider lookup/)).toBeInTheDocument();
    expect(screen.getByText("Investigation deadline")).toBeInTheDocument();
    expect(screen.getByText(/at or after it, never before/)).toBeInTheDocument();
  });

  it("says when nothing more will be checked", async () => {
    show({ ...BASE, state: "SUCCEEDED", outcome_class: "confirmed", next_check: {
      kind: "none_terminal", not_before: null, deadline: null,
      basis: "finished: no further check",
    } });
    expect(await screen.findByText("Nothing more will be checked or sent.")).toBeInTheDocument();
    expect(screen.queryByText("Investigation deadline")).toBeNull();
  });

  const ESCALATED = { ...BASE, state: "ESCALATED_UNRESOLVED", outcome_class: "unknown", transitions: [
    ...BASE.transitions, { seq: 4, from_state: "VERIFYING", to_state: "ESCALATED_UNRESOLVED",
      reason: "investigation_deadline_reached", actor: "recoverer", attempt_id: null, evidence: {},
      created_at: "2026-10-19T10:01:00Z" }] };

  it("says paced lookups continue for a limited time after escalation, and never resend", async () => {
    show({ ...ESCALATED, next_check: { kind: "post_escalation_lookup",
      not_before: new Date(Date.now() + 8_000).toISOString(), deadline: null,
      basis: "a bounded lookup after escalation; decisive evidence reopens verification" } });
    expect(await screen.findByText(/^A person decides next\. SafeAct still checks with the provider for a limited time; checks never resend\. Next check in about \d+ seconds\.$/)).toBeInTheDocument();
    expect(screen.getByText(/SafeAct still checks with the provider for a limited time \(next check in about \d+ seconds\); checks never resend\./)).toBeInTheDocument();
    expect(screen.getByText("A person decides · checks continue for now")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/one more check|No more automatic checks/);
  });

  it("says when automatic lookups have stopped after escalation", async () => {
    show({ ...ESCALATED, next_check: { kind: "beyond_horizon", not_before: null, deadline: null,
      basis: "past the post-escalation horizon: no automatic lookup; a reviewer can approve one REVERIFY" } });
    expect(await screen.findByText(/^No more automatic checks\. A person decides next; another lookup can be proposed for a reviewer to approve\. Nothing is resent\.$/)).toBeInTheDocument();
    expect(screen.getByText(/No more automatic checks; another lookup can be proposed/)).toBeInTheDocument();
    expect(screen.getByText("A person decides")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/still checks|checks continue/);
  });

  it("shows the plain state first, with the technical state in small text", async () => {
    show({ ...ESCALATED, next_check: null });
    const code = await screen.findByText("ESCALATED_UNRESOLVED", { selector: ".state-code" });
    expect(code.closest(".state-label")).toHaveTextContent("Needs review");
    expect(code).toHaveTextContent("Technical state: ESCALATED_UNRESOLVED");
  });

  it("hides a technical state that only repeats the label, but keeps it for screen readers", async () => {
    show({ ...BASE, state: "SUCCEEDED", outcome_class: "confirmed", success_provenance: "provider_verified" });
    const hidden = await screen.findByText("Technical state: SUCCEEDED");
    expect(hidden).toHaveClass("visually-hidden");
    expect(document.querySelector(".state-code")).toBeNull();
  });

  it("says plainly when the outcome is unknown", async () => {
    show({ ...BASE, state: "UNKNOWN", outcome_class: "unknown" }, undefined, "?tab=evidence");
    expect(await screen.findByText("Outcome unknown.")).toBeInTheDocument();
    expect(screen.getByText(/never proves an action failed/)).toBeInTheDocument();
  });

  it("renders provider text as text, never as markup", async () => {
    const { container } = show({ ...BASE, state: "UNKNOWN", outcome_class: "unknown" }, undefined, "?tab=evidence");
    expect(await screen.findByText(/onerror=alert/)).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
  });

  it("marks a reviewer-attested success apart from a verified one", async () => {
    show({ ...BASE, state: "SUCCEEDED", outcome_class: "confirmed", success_provenance: "human_attested" });
    expect(await screen.findByText(/attested by a reviewer, not verified/)).toBeInTheDocument();
  });

  // The attestation transition reuses the provider reason code, so wording must follow provenance.
  const SUCCESS = [
    { seq: 4, from_state: "VERIFYING", to_state: "ESCALATED_UNRESOLVED", reason: "investigation_deadline_reached",
      actor: "recoverer", attempt_id: null, evidence: {}, created_at: "2026-10-19T10:01:00Z" },
    { seq: 5, from_state: "ESCALATED_UNRESOLVED", to_state: "SUCCEEDED", reason: "provider_effect_confirmed",
      actor: "reviewer", attempt_id: null, evidence: {}, created_at: "2026-10-19T10:02:00Z" },
  ];

  it("never words a reviewer-attested success as provider confirmation", async () => {
    show({ ...BASE, state: "SUCCEEDED", outcome_class: "confirmed", success_provenance: "human_attested",
           transitions: [...BASE.transitions, ...SUCCESS] });
    expect(await screen.findByText("Succeeded · reviewer-attested.")).toBeInTheDocument();
    expect(screen.getByText("Attested by a reviewer")).toBeInTheDocument();
    expect(screen.getAllByText(/A reviewer attested that it was applied; the provider did not verify it/).length).toBeGreaterThan(0);
    const text = document.body.textContent ?? "";
    for (const claim of [/provider confirmed/i, /Confirmed by evidence/, /Evidence settles what happened/]) {
      expect(text).not.toMatch(claim);
    }
    await userEvent.click(screen.getByRole("tab", { name: "Timeline" }));
    expect(document.body.textContent).not.toMatch(/provider confirmed/i);
  });

  it("says a provider-verified success was verified with the provider", async () => {
    show({ ...BASE, state: "SUCCEEDED", outcome_class: "confirmed", success_provenance: "provider_verified",
           transitions: [...BASE.transitions, { ...SUCCESS[1]!, from_state: "VERIFYING", actor: "recoverer" }] });
    expect(await screen.findByText("Verified with the provider.")).toBeInTheDocument();
    expect(screen.getAllByText("Verified with the provider").length).toBeGreaterThan(0);
    expect(document.body.textContent).not.toMatch(/reviewer-attested|Attested by a reviewer/);
  });

  const STRIPE = {
    ...BASE, action_type: "refund_stripe_test_payment", connector_id: "stripe-test",
    resource_id: "pi_test_1", payload: { payment_intent: "pi_test_1", amount_minor: 500 },
    state: "SUCCEEDED", outcome_class: "confirmed", success_provenance: "provider_verified",
  };
  const stripeWith = (refund_status: unknown) => ({ ...STRIPE, attempts: [{ ...BASE.attempts[0], outcomes: [{
    ...BASE.attempts[0]!.outcomes[0], outcome: "confirmed_applied", evidence_kind: "applied",
    evidence_reference: "re_test_1", details: { provider: { refund_status } },
  }] }] });

  it("says a Stripe success means the refund was created, with the status Stripe reported", async () => {
    show(stripeWith("pending"));
    expect(await screen.findByText("Stripe accepted the request and created the refund.")).toBeInTheDocument();
    expect(screen.getByText(/Refund status Stripe reported at the time/)).toHaveTextContent(/Pending \(not a live value\)/);
    expect(screen.getByText(/Settlement to the customer is out of scope/)).toBeInTheDocument();
  });

  it("shows an unrecognized Stripe refund status verbatim, as text", async () => {
    show(stripeWith("<b>reversed_later</b>"));
    expect(await screen.findByText("<b>reversed_later</b>", { selector: "strong" })).toBeInTheDocument();
  });

  it("shows the reported refund status on the evidence card", async () => {
    show(stripeWith("failed"), undefined, "?tab=evidence");
    expect(await screen.findByText(/Refund status reported at response time/)).toHaveTextContent(/Failed/);
  });

  it("says when Stripe reported no status", async () => {
    show({ ...STRIPE, attempts: [] });
    expect(await screen.findByText(/Refund status Stripe reported at the time/)).toHaveTextContent(/Not recorded/);
  });

  it("adds no Stripe wording to a mock payment", async () => {
    show({ ...BASE, state: "SUCCEEDED", outcome_class: "confirmed", success_provenance: "provider_verified" });
    expect((await screen.findAllByText("Verified with the provider")).length).toBeGreaterThan(0);
    expect(screen.queryByText(/Stripe accepted/)).toBeNull();
    expect(screen.queryByText(/Refund status/)).toBeNull();
  });

  it("names an expired retry approval as resubmittable", async () => {
    show({ ...BASE, state: "FAILED_CONFIRMED", outcome_class: "confirmed",
           transitions: [{ ...BASE.transitions[0], to_state: "FAILED_CONFIRMED", reason: "retry_approval_expired" }] });
    // In the outcome summary; the timeline is on its own tab.
    expect((await screen.findAllByText(/retry approval expired/)).length).toBe(1);
    expect(screen.getByText("Not done", { selector: ".badge" })).toBeInTheDocument();
  });

  it("forgets the submit key only once the submitter sees the result", async () => {
    const owner = `${PRINCIPAL.tenant_id}.${PRINCIPAL.principal_id}`;
    rememberAction(owner, { request_key: "ui-mine", action_type: "refund_payment", provider_account_id: "mock-payments-account", contract_version: "v1", payload: BASE.payload });
    show({ ...BASE, request_key: "ui-mine", state: "RECORDED", outcome_class: "in_progress" },
         { submittedKey: "ui-mine", notice: "Recorded. Any retry must satisfy the connector's safety contract." });
    expect(await screen.findByText("Recorded. Any retry must satisfy the connector's safety contract.")).toBeInTheDocument();
    expect(sessionStorage.getItem(actionStorage(owner))).toBeNull();
  });

  it("never forgets a key that belongs to a newer form", async () => {
    const owner = `${PRINCIPAL.tenant_id}.${PRINCIPAL.principal_id}`;
    rememberAction(owner, { request_key: "ui-newer", action_type: "refund_payment", provider_account_id: "mock-payments-account", contract_version: "v1", payload: BASE.payload });
    show({ ...BASE, state: "RECORDED", outcome_class: "in_progress" }, { submittedKey: "ui-older" });
    expect(await screen.findByRole("heading", { name: "Refund $15.00 on Charge #1002" })).toBeInTheDocument();
    expect(JSON.parse(sessionStorage.getItem(actionStorage(owner))!).request_key).toBe("ui-newer");
  });
});

describe("action detail tabs", () => {
  it("moves between tabs with the arrow keys and keeps the story first", async () => {
    show({ ...BASE, state: "UNKNOWN", outcome_class: "unknown" });
    const overview = await screen.findByRole("tab", { name: "Overview" });
    expect(overview).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("heading", { name: "What happened" })).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Progress" })).toHaveTextContent(/Checked.*in progress/);
    overview.focus();
    await userEvent.keyboard("{ArrowRight}");
    const timeline = screen.getByRole("tab", { name: "Timeline" });
    expect(timeline).toHaveFocus();
    expect(timeline).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("No response from the provider");
    await userEvent.keyboard("{End}");
    expect(screen.getByRole("tab", { name: "Technical details" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("ui-1")).toBeInTheDocument();
  });
});
