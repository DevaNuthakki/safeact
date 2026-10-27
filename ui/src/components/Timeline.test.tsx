import { render, screen } from "@testing-library/react";

import type { Transition } from "../api/types";
import { Timeline, statePath, stepText, timelineNodes } from "./Timeline";

let seq = 0;
function t(from: string, to: string, reason: string): Transition {
  seq += 1;
  return { seq, from_state: from, to_state: to, reason, actor: "w", attempt_id: null, evidence: {},
           created_at: "2026-10-25T10:00:00Z" };
}

const ESCALATED = [
  t("RECORDED", "CLAIMED", "claimed"), t("CLAIMED", "DISPATCHING", "dispatch_risk_committed"),
  t("DISPATCHING", "UNKNOWN", "ambiguous_no_response"),
  t("UNKNOWN", "VERIFYING", "verify_with_provider"), t("VERIFYING", "UNKNOWN", "insufficient_safe_retry_evidence"),
  t("UNKNOWN", "VERIFYING", "verify_with_provider"), t("VERIFYING", "UNKNOWN", "insufficient_safe_retry_evidence"),
  t("UNKNOWN", "VERIFYING", "verify_with_provider"),
  t("VERIFYING", "ESCALATED_UNRESOLVED", "investigation_deadline_reached"),
];

describe("timeline", () => {
  it("speaks plainly, falling back to the state's meaning", () => {
    expect(stepText({ to_state: "UNKNOWN", reason: "ambiguous_no_response" }))
      .toBe("No response from the provider, checking before doing anything else.");
    expect(stepText({ to_state: "FAILED_CONFIRMED", reason: "retry_approval_expired" }))
      .toMatch(/retry approval expired/);
    expect(stepText({ to_state: "SUCCEEDED", reason: "some_new_reason" })).toBe("Completed. Nothing more will be sent.");
    expect(stepText({ to_state: "MYSTERY", reason: "some_new_reason" })).toBe("some new reason");
  });

  it("words a success by its provenance, not by the reused reason code", () => {
    const success = { to_state: "SUCCEEDED", reason: "provider_effect_confirmed" };
    expect(stepText(success, "human_attested")).toMatch(/^A reviewer attested that it was applied; the provider did not verify it/);
    expect(stepText(success, "provider_verified")).toMatch(/^The provider's evidence shows it was applied/);
    expect(stepText(success, "human_attested")).not.toMatch(/provider confirmed/i);
  });

  it("folds repeated inconclusive lookups into one node and keeps every step", () => {
    const nodes = timelineNodes(ESCALATED);
    expect(nodes.map((n) => n.kind)).toEqual(["step", "step", "step", "checks", "step"]);
    render(<Timeline transitions={ESCALATED} recordedAt="2026-10-25T10:00:00Z" />);
    expect(screen.getByText("No decisive answer from 2 provider lookups")).toBeInTheDocument();
    expect(screen.getByText("Show all 5 steps")).toBeInTheDocument();
    expect(screen.getAllByText("Asking the provider what happened.")).toHaveLength(3);
  });

  it("keeps a short check as ordinary steps", () => {
    const short = [t("DISPATCHING", "UNKNOWN", "ambiguous_no_response"),
      t("UNKNOWN", "VERIFYING", "verify_with_provider"), t("VERIFYING", "SUCCEEDED", "provider_effect_confirmed")];
    expect(timelineNodes(short).every((n) => n.kind === "step")).toBe(true);
  });

  it("shows the path through distinct states", () => {
    expect(statePath(ESCALATED)).toEqual(
      ["RECORDED", "CLAIMED", "DISPATCHING", "UNKNOWN", "VERIFYING", "ESCALATED_UNRESOLVED"]);
  });
});
