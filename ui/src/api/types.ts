// Response shapes. Typed endpoints come from the generated OpenAPI
// schema; the JSON views built in SQL (operation detail, proposals) are
// described here by hand, matching storage/lifecycle_sql v0012-v0015.

import type { components } from "./schema";

export type Principal = components["schemas"]["PrincipalView"];
export type OperationSummary = components["schemas"]["OperationSummary"];
export type OperationPage = components["schemas"]["OperationPage"];
export type Grant = components["schemas"]["GrantView"];
export type Admitted = components["schemas"]["Admitted"];
export type CancelResult = components["schemas"]["CancelResult"];
export type WorkflowAdmitted = components["schemas"]["WorkflowAdmitted"];
export type CreateWorkflow = components["schemas"]["CreateWorkflow"];

/** Tenant-scoped JSON view returned by safeact_api_workflow (0012). */
export interface WorkflowDetail {
  workflow_id: string;
  request_key: string;
  workflow_type: string;
  created_by: string;
  created_at: string;
  plan: {
    order: { resource_id: string; payload_canonical: string };
    compensation: { policy: string };
  };
  status: string;
  steps: {
    step: string;
    operation_id: string;
    state: string;
    compensates_operation_id: string | null;
  }[];
}

export type Role = "requester" | "investigator" | "reviewer";

export type OutcomeClass = "confirmed" | "unknown" | "in_progress" | "cancelled";

export interface EvidenceRow {
  outcome: string;
  evidence_kind: string | null;
  evidence_reference: string | null;
  observed_by: string;
  observed_seq: number;
  provenance: "system" | "human_attested";
  /** Provider-supplied text: untrusted, rendered as text only. */
  details: Record<string, unknown>;
  created_at: string;
}

export interface Attempt {
  attempt_id: string;
  ordinal: number;
  created_at: string;
  outcomes: EvidenceRow[];
}

export interface Transition {
  seq: number;
  from_state: string;
  to_state: string;
  reason: string;
  actor: string;
  attempt_id: string | null;
  evidence: Record<string, unknown>;
  created_at: string;
}

export interface Contract {
  connector_id: string;
  version: string;
  side_effect_class: string;
  native_idempotency: boolean;
  key_retention_seconds: number | null;
  key_binds_parameters: boolean | null;
  key_protects_concurrent: boolean | null;
  lookup_semantics: string;
  request_deadline_seconds: number;
  investigation_window_seconds: number;
  max_attempts: number;
  max_reopens: number;
  compensation_action: string | null;
  contract_reference: string;
}

/** When the next check happens (0019); times are "not before". */
export interface NextCheck {
  kind: string;
  not_before: string | null;
  deadline: string | null;
  basis: string;
}

export interface OperationDetail {
  operation_id: string;
  request_key: string;
  action_type: string;
  resource_id: string;
  provider_account_id: string;
  connector_id: string;
  contract_version: string;
  payload: Record<string, unknown>;
  payload_sha256: string;
  provider_key_protected: boolean;
  created_at: string;
  requested_by: string | null;
  state: string;
  seq: number;
  outcome_class: OutcomeClass;
  next_check: NextCheck | null;
  success_provenance: "provider_verified" | "human_attested" | null;
  workflow: { workflow_id: string; step: string } | null;
  contract: Contract | null;
  transitions: Transition[];
  attempts: Attempt[];
  cancellation_requests: {
    requested_by: string;
    details: Record<string, unknown>;
    created_at: string;
  }[];
}

export type ProposalAction = "REVERIFY" | "PROTECTED_RETRY" | "ATTEST_APPLIED";
export type ProposalStatus = "open" | "stale" | "expired" | "approved" | "rejected";

export interface Proposal {
  proposal_id: string;
  operation_id: string;
  action: ProposalAction;
  rationale: string;
  attested_reference: string | null;
  proposed_by: string;
  created_at: string;
  expires_at: string;
  status: ProposalStatus;
  binding: {
    seq: number;
    state: string;
    payload_sha256: string;
    evidence_digest: string;
    connector_id: string;
    contract_version: string;
  };
  binding_digest: string;
  current: {
    seq: number;
    state: string;
    evidence_digest: string;
    eligibility: string | null;
  };
  decision: {
    decision: "approve" | "reject";
    decided_by: string;
    note: string | null;
    created_at: string;
  } | null;
}

export interface DecisionResult {
  outcome: "approved" | "rejected" | "replayed";
  reason: string | null;
  proposal: Proposal;
}
