import type { NextCheck as NextCheckData } from "../api/types";
import { relative, useNow } from "./time";

// What happens next, in plain words. Every time is a "not before": the
// reconciler acts at or after it, never before.
export const KINDS: Record<string, { title: string; when: string }> = {
  none_terminal: { title: "No further check", when: "" },
  awaiting_dispatch: { title: "Waiting for a worker", when: "Claims resume" },
  awaiting_retry: { title: "Protected retry", when: "Not before" },
  dispatch_in_progress: { title: "Being sent", when: "Investigated if no answer by" },
  awaiting_approval: { title: "Waiting for a reviewer", when: "Decided without a retry at" },
  scheduled_lookup: { title: "Next provider lookup", when: "Not before" },
  post_escalation_lookup: { title: "Next bounded lookup", when: "Not before" },
  beyond_horizon: { title: "No automatic lookup", when: "" },
  manual_review_only: { title: "A person decides", when: "" },
};

/** "in about 8 seconds"; a time already passed happens at the next pass. */
export function naturalWhen(target: Date, now: number) {
  const seconds = Math.round((target.getTime() - now) / 1000);
  if (seconds <= 0) return "shortly";
  if (seconds < 5) return "in a moment";
  if (seconds < 90) return `in about ${seconds} seconds`;
  if (seconds < 5400) return `in about ${Math.round(seconds / 60)} minutes`;
  return `at ${target.toLocaleString()}`;
}

/** One sentence for the next check, or null when nothing more happens. */
export function nextCheckSentence(check: NextCheckData | null | undefined, now: number) {
  if (!check) return null;
  const notBefore = check.not_before ? ` ${naturalWhen(new Date(check.not_before), now)}` : "";
  const deadline = check.deadline ? ` ${naturalWhen(new Date(check.deadline), now)}` : "";
  switch (check.kind) {
    case "none_terminal": return "Nothing more will be checked or sent.";
    case "awaiting_dispatch": return `Waiting for a worker to send it${notBefore}.`;
    case "awaiting_retry": return `A protected retry may run${notBefore}.`;
    case "dispatch_in_progress":
      return `Waiting for the provider's answer. If none arrives, SafeAct checks with the provider${deadline}.`;
    case "awaiting_approval": return "Waiting for a reviewer before any retry.";
    case "scheduled_lookup": return `SafeAct will ask the provider again${notBefore}.`;
    case "post_escalation_lookup":
      return `A person decides next. SafeAct still checks with the provider for a limited time; checks never resend. Next check${notBefore}.`;
    case "beyond_horizon":
      return "No more automatic checks. A person decides next; another lookup can be proposed for a reviewer to approve. Nothing is resent.";
    case "manual_review_only": return "A person decides next.";
    default: return `Next check${notBefore}.`;
  }
}

/** For an escalated action's headline: what still happens automatically, if anything. */
export function escalationFollowUp(check: NextCheckData | null | undefined, now: number) {
  if (check?.kind === "post_escalation_lookup") {
    const when = check.not_before ? ` ${naturalWhen(new Date(check.not_before), now)}` : "";
    return `SafeAct still checks with the provider for a limited time (next check${when}); checks never resend.`;
  }
  if (check?.kind === "beyond_horizon") {
    return "No more automatic checks; another lookup can be proposed for a reviewer to approve.";
  }
  return null;
}

export function EscalationFollowUp({ check }: { check: NextCheckData | null | undefined }) {
  const now = useNow(Boolean(check?.not_before));
  const text = escalationFollowUp(check, now);
  return text ? <span> {text}</span> : null;
}

export function NextCheckLine({ check }: { check: NextCheckData | null | undefined }) {
  const now = useNow(Boolean(check?.not_before || check?.deadline));
  const sentence = nextCheckSentence(check, now);
  return sentence ? <p className="next-check">{sentence}</p> : null;
}

/** The raw schedule, for Technical details. */
export function NextCheck({ check }: { check: NextCheckData | null | undefined }) {
  const notBefore = check?.not_before ? new Date(check.not_before) : null;
  const deadline = check?.deadline ? new Date(check.deadline) : null;
  const now = useNow(Boolean(notBefore || deadline));
  if (!check) return null;
  const kind = KINDS[check.kind] ?? { title: "Next check", when: "Not before" };
  return (
    <>
      <dt>Next check</dt>
      <dd><code>{check.kind}</code> ({kind.title}). {check.basis}</dd>
      {notBefore && (
        <>
          <dt>{kind.when || "Not before"}</dt>
          <dd>
            <time dateTime={check.not_before ?? undefined}>{notBefore.toLocaleString()}</time>{" "}
            <span className="hint">({relative(notBefore, now)}; the system acts at or after it, never before)</span>
          </dd>
        </>
      )}
      {deadline && (
        <>
          <dt>Investigation deadline</dt>
          <dd>
            <time dateTime={check.deadline ?? undefined}>{deadline.toLocaleString()}</time>{" "}
            <span className="hint">({relative(deadline, now)}; unresolved work escalates to a person)</span>
          </dd>
        </>
      )}
    </>
  );
}
