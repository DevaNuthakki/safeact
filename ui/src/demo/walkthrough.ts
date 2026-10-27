import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";
import type { OperationDetail, Proposal } from "../api/types";

export interface WalkthroughCases {
  schema_version: 1;
  prepared_at: string;
  refund: { operation_id: string };
  email: { operation_id: string; proposal_id: string };
  order: { operation_id: string; proposal_id: string };
  agent_refusal: {
    captured_at: string; operation_id: string;
    tool: "propose_resolution"; action: "ATTEST_APPLIED";
    error: "invalid_arguments"; message: string;
  };
  stale_refusal: {
    captured_at: string; proposal_id: string;
    http_status: 409; code: "stale_approval"; reason: string;
  };
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const object = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;

/** A public case index contains identifiers and captured refusals, never keys. */
export function parseWalkthrough(value: unknown): WalkthroughCases {
  if (!object(value) || value.schema_version !== 1
      || typeof value.prepared_at !== "string" || !Number.isFinite(Date.parse(value.prepared_at))) {
    throw new Error("The walkthrough case index is not valid.");
  }
  for (const name of ["refund", "email", "order"] as const) {
    const entry = value[name];
    if (!object(entry) || typeof entry.operation_id !== "string" || !UUID.test(entry.operation_id)
        || (name !== "refund" && (typeof entry.proposal_id !== "string" || !UUID.test(entry.proposal_id)))) {
      throw new Error("The walkthrough case identifiers are not valid.");
    }
  }
  const email = value.email as Record<string, unknown>;
  const order = value.order as Record<string, unknown>;
  const agent = value.agent_refusal;
  const stale = value.stale_refusal;
  if (!object(agent) || agent.operation_id !== email.operation_id || agent.tool !== "propose_resolution"
      || agent.action !== "ATTEST_APPLIED" || agent.error !== "invalid_arguments" || typeof agent.message !== "string"
      || typeof agent.captured_at !== "string" || !Number.isFinite(Date.parse(agent.captured_at))
      || !object(stale) || stale.proposal_id !== order.proposal_id || stale.http_status !== 409
      || stale.code !== "stale_approval" || !["evidence_changed", "operation_changed"].includes(String(stale.reason))
      || typeof stale.captured_at !== "string" || !Number.isFinite(Date.parse(stale.captured_at))) {
    throw new Error("The captured refusals do not match these walkthrough cases.");
  }
  return value as unknown as WalkthroughCases;
}

export function useWalkthroughCases() {
  return useQuery({
    queryKey: ["walkthrough", "cases"],
    queryFn: async ({ signal }) => {
      const response = await fetch("/demo-cases.json", { headers: { Accept: "application/json" }, signal, cache: "no-store" });
      if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) {
        throw new Error("The walkthrough has not been prepared for this server.");
      }
      return parseWalkthrough(await response.json());
    },
    retry: false,
    staleTime: 30_000,
  });
}

export function useStoryAction(operationId?: string) {
  return useQuery({
    queryKey: ["action", operationId],
    queryFn: ({ signal }) => api<OperationDetail>(`/actions/${operationId}`, { signal }),
    enabled: Boolean(operationId), refetchInterval: 5_000,
  });
}

export function useStoryProposal(proposalId?: string) {
  return useQuery({
    queryKey: ["proposals", "detail", proposalId],
    queryFn: ({ signal }) => api<Proposal>(`/proposals/${proposalId}`, { signal }),
    enabled: Boolean(proposalId), refetchInterval: 5_000,
  });
}

export const DEMO_AGENT = "5afe0000-0000-4000-8000-000000000105";

/** The prepared refund: $2,450.00. A blind second refund would make it $4,900.00. */
export const STORY_REFUND_CENTS = 245_000;

export function matchesStoryAction(op: OperationDetail, kind: "refund" | "email" | "order") {
  const expected = {
    refund: ["refund_payment", "mock-payments", "ch_north_1002"],
    email: ["send_refund_confirmation", "mock-email", "cust_north_3002"],
    order: ["create_replacement_order", "mock-crm", "case_north_2002"],
  }[kind];
  return op.action_type === expected[0] && op.connector_id === expected[1] && op.resource_id === expected[2]
    && (kind !== "refund" || op.payload.amount_minor === STORY_REFUND_CENTS)
    && (kind !== "email" || op.requested_by === DEMO_AGENT);
}

export function matchingAttestation(proposal: Proposal, operationId: string) {
  return proposal.operation_id === operationId && proposal.action === "ATTEST_APPLIED" && Boolean(proposal.attested_reference);
}
