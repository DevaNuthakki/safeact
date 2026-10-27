import type { ReactNode } from "react";

import { ApiError, NetworkError } from "../api/client";
import { Icon } from "./Icon";
import type { IconName } from "./Icon";

export function Loading({ label = "Loading…", rows }: { label?: string; rows?: number }) {
  if (rows) {
    return (
      <div role="status" aria-live="polite">
        <span className="visually-hidden">{label}</span>
        <div className="skeleton" aria-hidden="true">
          {Array.from({ length: rows }, (_, i) => (
            <div key={i} className="skeleton__row"><div className="skeleton__bar" style={{ width: `${70 - i * 8}%` }} /></div>
          ))}
        </div>
      </div>
    );
  }
  return (
    <p className="loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />{label}
    </p>
  );
}

export function Empty({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <Icon name="inbox" />
      <p className="empty__text">{children}</p>
      {action}
    </div>
  );
}

/** Explains a failed request without implying what happened remotely. */
export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  let message = "Something went wrong. Try again.";
  if (error instanceof ApiError) {
    message = error.status === 404 ? "Not found, or you cannot see it." : error.message;
  } else if (error instanceof NetworkError) {
    message = error.message;
  }
  return (
    <div className="notice bad stack-sm" role="alert">
      <p>{message}</p>
      {retry && <p><button type="button" className="secondary" onClick={retry}>Try again</button></p>}
    </div>
  );
}

// Plain-language meaning of each lifecycle state. "Unknown" is a real,
// honest state: the system does not yet know whether the effect happened.
// Tone is text plus icon plus color, never color alone. Escalation is a
// safe outcome that needs a person, not a failure: amber, not red.
const STATES: Record<string, { label: string; tone: string; icon: IconName; meaning: string }> = {
  RECORDED: { label: "Recorded", tone: "info", icon: "clock", meaning: "Saved; waiting for a worker." },
  CLAIMED: { label: "Claimed", tone: "info", icon: "clock", meaning: "A worker is preparing to send it." },
  DISPATCHING: { label: "Sending", tone: "info", icon: "clock", meaning: "Sent to the provider; waiting for its answer." },
  SUCCEEDED: { label: "Succeeded", tone: "ok", icon: "check", meaning: "Completed. Nothing more will be sent." },
  FAILED_CONFIRMED: { label: "Not done", tone: "bad", icon: "x", meaning: "Evidence proves it was not applied." },
  UNKNOWN: { label: "Unknown", tone: "warn", icon: "search", meaning: "It may or may not have happened; being checked." },
  VERIFYING: { label: "Verifying", tone: "warn", icon: "search", meaning: "Asking the provider what happened." },
  RETRY_ELIGIBLE: { label: "Retry allowed", tone: "info", icon: "clock", meaning: "A protected retry may run." },
  ESCALATED_UNRESOLVED: { label: "Needs review", tone: "warn", icon: "alert", meaning: "Still unknown after investigation; a person must look." },
  CANCELLED_BEFORE_CLAIM: { label: "Cancelled", tone: "neutral", icon: "minus", meaning: "Cancelled before anything was sent." },
};

export function stateInfo(state: string) {
  return STATES[state] ?? { label: state, tone: "neutral", icon: "dot" as IconName, meaning: "" };
}

export type SuccessProvenance = "provider_verified" | "human_attested" | null | undefined;

// How a success was established. A reviewer's attestation is never worded as
// provider verification: the provider did not confirm it.
export function successWording(provenance: SuccessProvenance) {
  if (provenance === "provider_verified") {
    return { title: "Verified with the provider", detail: "Verified with the provider",
      text: "The provider's evidence shows it was applied. Nothing more will be sent." };
  }
  if (provenance === "human_attested") {
    return { title: "Succeeded · reviewer-attested", detail: "Attested by a reviewer",
      text: "A reviewer attested that it was applied; the provider did not verify it. Nothing more will be sent." };
  }
  return { title: "Succeeded", detail: "Completed", text: "Completed. Nothing more will be sent." };
}

/** The lifecycle code, as small secondary text under a plain-language label. */
export function StateCode({ state, label }: { state: string; label: string }) {
  // Shown only when it says more than the label ("Needs review" vs ESCALATED_UNRESOLVED);
  // SUCCEEDED under "Succeeded" is noise. Screen readers always get it.
  if (state.replaceAll("_", " ").toLowerCase() === label.toLowerCase()) {
    return <span className="visually-hidden">Technical state: {state}</span>;
  }
  return <span className="state-code"><span className="visually-hidden">Technical state: </span>{state}</span>;
}

/** Plain words first; the technical state stays visible in small text below. */
export function StateBadge({ state, label }: { state: string; label?: string }) {
  const info = stateInfo(state);
  return (
    <span className="state-label">
      <span className={`badge ${info.tone}`} title={info.meaning}><Icon name={info.icon} />{label ?? info.label}</span>
      <StateCode state={state} label={info.label} />
    </span>
  );
}

export function Badge({ tone, icon, children, title }: {
  tone: string; icon: IconName; children: ReactNode; title?: string;
}) {
  return <span className={`badge ${tone}`} title={title}><Icon name={icon} />{children}</span>;
}

const REASONS: Record<string, string> = {
  retry_approval_expired: "Not done: the retry approval expired before a reviewer approved it. You can submit it again.",
  cancelled_before_claim: "Cancelled before any attempt was made.",
  retry_approved_by_reviewer: "A reviewer approved a protected retry.",
  reopen_recorded_evidence: "Reopened because new decisive evidence was recorded.",
  reopen_new_evidence: "Reopened because a lookup found decisive evidence.",
  ambiguous_no_response: "No response from the provider: the outcome is unknown.",
  ambiguous_error_after_send: "An error came back after sending: the outcome is unknown.",
  rejected_before_execution_retry_pending: "The provider rejected it before doing anything.",
};

/** The plain-language text for a known reason, if there is one. */
export function knownReason(reason: string): string | undefined {
  return REASONS[reason];
}

export function reasonText(reason: string) {
  return REASONS[reason] ?? reason.replaceAll("_", " ");
}
