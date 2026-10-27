/** Synthetic read responses for UI tests; never loaded by the application. */
import type { OperationDetail, Proposal } from "../api/types";
import type { WalkthroughCases } from "../demo/walkthrough";
import { DEMO_AGENT } from "../demo/walkthrough";

const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const at = "2026-10-26T12:00:00Z";
export const CASES: WalkthroughCases = {
  schema_version: 1, prepared_at: at,
  refund: { operation_id: id(1) }, email: { operation_id: id(2), proposal_id: id(4) },
  order: { operation_id: id(3), proposal_id: id(5) },
  agent_refusal: { captured_at: at, operation_id: id(2), tool: "propose_resolution",
    action: "ATTEST_APPLIED", error: "invalid_arguments", message: "Agent proposal action is not allowed." },
  stale_refusal: { captured_at: at, proposal_id: id(5), http_status: 409, code: "stale_approval", reason: "evidence_changed" },
};

const base: OperationDetail = {
  operation_id: id(2), request_key: "test-only", action_type: "send_refund_confirmation",
  resource_id: "cust_north_3002", provider_account_id: "mock-email-account", connector_id: "mock-email",
  contract_version: "v1", payload: {}, payload_sha256: "a".repeat(64), provider_key_protected: false,
  created_at: at, requested_by: DEMO_AGENT, state: "ESCALATED_UNRESOLVED", seq: 4,
  outcome_class: "unknown", next_check: null, success_provenance: null, workflow: null,
  contract: null, cancellation_requests: [],
  transitions: [{ seq: 3, from_state: "DISPATCHING", to_state: "UNKNOWN", reason: "ambiguous_no_response",
    actor: "worker", attempt_id: "attempt-1", evidence: {}, created_at: at }],
  attempts: [{ attempt_id: "attempt-1", ordinal: 1, created_at: at, outcomes: [] }],
};
export const EMAIL: OperationDetail = { ...base };
export const ORDER: OperationDetail = { ...base, operation_id: id(3), connector_id: "mock-crm",
  action_type: "create_replacement_order", resource_id: "case_north_2002", requested_by: "requester" };
export const REFUND: OperationDetail = { ...base, operation_id: id(1), connector_id: "mock-payments",
  action_type: "refund_payment", resource_id: "ch_north_1002", payload: { amount_minor: 245000 },
  requested_by: "requester", state: "SUCCEEDED", outcome_class: "confirmed", success_provenance: "provider_verified" };
export function proposal(email = true): Proposal {
  return {
    proposal_id: email ? id(4) : id(5), operation_id: email ? id(2) : id(3), action: "ATTEST_APPLIED",
    rationale: "Synthetic test evidence", attested_reference: email ? "msg_demo_delivered_1" : "crm_order_seen_1",
    proposed_by: "investigator", created_at: at, expires_at: new Date(Date.now() + 600_000).toISOString(),
    status: email ? "open" : "stale", decision: null,
    binding: { seq: 4, state: "ESCALATED_UNRESOLVED", payload_sha256: "a".repeat(64),
      evidence_digest: "b".repeat(64), connector_id: email ? "mock-email" : "mock-crm", contract_version: "v1" },
    binding_digest: "c".repeat(64),
    current: { seq: 4, state: "ESCALATED_UNRESOLVED", evidence_digest: (email ? "b" : "d").repeat(64), eligibility: null },
  };
}
