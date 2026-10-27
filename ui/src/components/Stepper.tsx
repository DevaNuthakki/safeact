import type { Transition } from "../api/types";
import { Icon } from "./Icon";
import type { IconName } from "./Icon";
import { successWording } from "./States";
import type { SuccessProvenance } from "./States";
import { statePath } from "./Timeline";

// Where an action is, as a few plain phases: recorded, sent, checked
// (only when the outcome was uncertain) and the result. Each phase says
// its status in words, not color alone.

type Status = "done" | "current" | "upcoming";
interface Phase { label: string; detail: string; icon: IconName; status: Status; tone: string }

const FINAL: Record<string, { label: string; detail: string; icon: IconName; tone: string }> = {
  SUCCEEDED: { label: "Succeeded", detail: "Completed", icon: "check", tone: "ok" },
  FAILED_CONFIRMED: { label: "Not done", detail: "Proven not applied", icon: "x", tone: "bad" },
  ESCALATED_UNRESOLVED: { label: "Needs review", detail: "A person decides", icon: "alert", tone: "warn" },
  CANCELLED_BEFORE_CLAIM: { label: "Cancelled", detail: "Nothing was sent", icon: "minus", tone: "neutral" },
};

const STATUS_TEXT: Record<Status, string> = { done: "done", current: "in progress", upcoming: "not yet" };

export function phases(
  state: string, transitions: Transition[], provenance?: SuccessProvenance, nextCheck?: string,
): Phase[] {
  const path = statePath(transitions);
  const sent = path.includes("DISPATCHING");
  const checked = path.includes("UNKNOWN") || path.includes("VERIFYING");
  const base = FINAL[state];
  // Success says how it was established: verified with the provider or attested by a reviewer.
  const final = base && state === "SUCCEEDED" ? { ...base, detail: successWording(provenance).detail }
    // Escalation hands the decision to a person; bounded lookups may still run for a while.
    : base && state === "ESCALATED_UNRESOLVED" && nextCheck === "post_escalation_lookup"
      ? { ...base, detail: "A person decides · checks continue for now" } : base;
  const result: Phase[] = [
    { label: "Recorded", detail: "Saved before sending", icon: "file", status: "done", tone: "info" },
  ];
  if (state === "CANCELLED_BEFORE_CLAIM") {
    return [...result, { ...final!, status: "done" }];
  }
  const sending = ["CLAIMED", "DISPATCHING", "RETRY_ELIGIBLE"].includes(state);
  result.push({
    label: "Sent", detail: sent ? "Sent to the provider" : "Waiting to send", icon: "send", tone: "info",
    status: sending ? "current" : sent ? "done" : "upcoming",
  });
  if (checked) {
    const checking = ["UNKNOWN", "VERIFYING"].includes(state);
    result.push({
      label: "Checked", detail: checking ? "Asking the provider" : "Outcome was uncertain", icon: "search",
      tone: "warn", status: checking ? "current" : "done",
    });
  }
  result.push(final
    ? { ...final, status: "done" }
    : { label: "Result", detail: "Not known yet", icon: "dot", tone: "neutral", status: "upcoming" });
  return result;
}

export function Stepper({ state, transitions, provenance, nextCheck }: {
  state: string; transitions: Transition[]; provenance?: SuccessProvenance; nextCheck?: string;
}) {
  return (
    <ol className="stepper" aria-label="Progress">
      {phases(state, transitions, provenance, nextCheck).map((phase) => (
        <li key={phase.label} className={`stepper__step ${phase.status} ${phase.status === "upcoming" ? "" : phase.tone}`}>
          <span className="stepper__icon"><Icon name={phase.icon} /></span>
          <span className="stepper__text">
            <span className="stepper__label">{phase.label}</span>
            <span className="stepper__detail">{phase.detail}</span>
            <span className="visually-hidden"> ({STATUS_TEXT[phase.status]})</span>
          </span>
        </li>
      ))}
    </ol>
  );
}
