import type { Transition } from "../api/types";
import { Raw } from "./EvidenceCard";
import { Icon } from "./Icon";
import { knownReason, reasonText, stateInfo, successWording } from "./States";
import type { SuccessProvenance } from "./States";
import { TechnicalDetails } from "./TechnicalDetails";
import { RelativeTime } from "./time";

// The state path in plain language. Each step says what happened and
// what SafeAct does next; the raw reason code, actor and version are
// folded into each step's Technical details for anyone checking the record.

const AMBIGUOUS = "The provider's answer was ambiguous, checking before doing anything else.";

const BY_REASON: Record<string, string> = {
  claimed: "A worker picked it up. Nothing has been sent yet.",
  dispatch_risk_committed: "Recorded that it is about to be sent, then sent it to the provider.",
  provider_confirmed: "The provider confirmed it was applied. Nothing more will be sent.",
  ambiguous_no_response: "No response from the provider, checking before doing anything else.",
  ambiguous_error_after_send: "An error came back after sending, checking before doing anything else.",
  ambiguous_key_conflict: AMBIGUOUS,
  provider_in_flight_conflict: AMBIGUOUS,
  request_deadline_elapsed_before_send: AMBIGUOUS,
  ambiguous_rate_limited: AMBIGUOUS,
  verify_with_provider: "Asking the provider what happened.",
  insufficient_safe_retry_evidence:
    "The provider could not say either way. Nothing is resent without proof; it will check again.",
  provider_effect_confirmed: "The provider confirmed the effect was applied. Nothing more will be sent.",
  investigation_deadline_reached:
    "Still unknown when the investigation window closed. A person must review it; nothing will be resent.",
};

/** The attestation path reuses a provider reason code, so a success is worded by its provenance. */
export function stepText(t: Pick<Transition, "to_state" | "reason">, provenance?: SuccessProvenance) {
  if (t.to_state === "SUCCEEDED" && provenance) return successWording(provenance).text;
  return BY_REASON[t.reason] ?? knownReason(t.reason)
    ?? (stateInfo(t.to_state).meaning || reasonText(t.reason));
}

const isCheck = (t: Transition) =>
  (t.from_state === "UNKNOWN" && t.to_state === "VERIFYING")
  || (t.from_state === "VERIFYING" && t.to_state === "UNKNOWN");

type Node = { kind: "step"; t: Transition } | { kind: "checks"; items: Transition[] };

/** Repeated inconclusive lookups become one node; every step stays listed inside it. */
export function timelineNodes(transitions: Transition[]): Node[] {
  const nodes: Node[] = [];
  let run: Transition[] = [];
  const flush = () => {
    if (run.length >= 3) nodes.push({ kind: "checks", items: run });
    else nodes.push(...run.map((t) => ({ kind: "step" as const, t })));
    run = [];
  };
  for (const t of transitions) {
    if (isCheck(t)) run.push(t);
    else {
      flush();
      nodes.push({ kind: "step", t });
    }
  }
  flush();
  return nodes;
}

/** Distinct states in the order first reached. */
export function statePath(transitions: Transition[]) {
  const states = transitions.length ? [transitions[0]!.from_state, ...transitions.map((t) => t.to_state)] : ["RECORDED"];
  return [...new Set(states)];
}

function Meta({ t }: { t: Transition }) {
  return (
    <>
      <p className="timeline__meta"><RelativeTime value={t.created_at} /></p>
      <TechnicalDetails>
        <dl className="facts">
          <dt>Change</dt><dd><code>{t.from_state}</code> → <code>{t.to_state}</code></dd>
          <dt>Reason code</dt><dd><code>{t.reason}</code></dd>
          <dt>Recorded by</dt><dd><code>{t.actor}</code></dd>
          <dt>Version</dt><dd>{t.seq}</dd>
          {t.attempt_id && <><dt>Attempt</dt><dd><code>{t.attempt_id}</code></dd></>}
          {Object.keys(t.evidence).length > 0 && <><dt>Evidence</dt><dd><Raw value={t.evidence} /></dd></>}
        </dl>
      </TechnicalDetails>
    </>
  );
}

export function Timeline({ transitions, recordedAt, provenance }: {
  transitions: Transition[]; recordedAt: string; provenance?: SuccessProvenance;
}) {
  const recorded = (
    <li>
      <span className="timeline__node info"><Icon name="clock" /></span>
      <p className="timeline__title">Recorded</p>
      <p className="timeline__text">Saved durably before anything was sent.</p>
      <p className="timeline__meta"><RelativeTime value={recordedAt} /></p>
    </li>
  );
  if (transitions.length === 0) {
    return <ol className="timeline">{recorded}</ol>;
  }
  return (
    <>
      <ol className="timeline">
        {recorded}
        {timelineNodes(transitions).map((node) => {
          if (node.kind === "step") {
            const info = stateInfo(node.t.to_state);
            return (
              <li key={node.t.seq}>
                <span className={`timeline__node ${info.tone}`}><Icon name={info.icon} /></span>
                <p className="timeline__title">{info.label}</p>
                <p className="timeline__text">{stepText(node.t, provenance)}</p>
                <Meta t={node.t} />
              </li>
            );
          }
          const lookups = node.items.filter((t) => t.to_state === "UNKNOWN").length;
          const first = node.items[0]!;
          const last = node.items.at(-1)!;
          return (
            <li key={`checks-${first.seq}`}>
              <span className="timeline__node warn"><Icon name="search" /></span>
              <p className="timeline__title">
                No decisive answer from {lookups} provider {lookups === 1 ? "lookup" : "lookups"}
              </p>
              <p className="timeline__text">Nothing was sent again while the outcome stayed unknown.</p>
              <p className="timeline__meta">
                <span>latest <RelativeTime value={last.created_at} /></span>
              </p>
              <details className="timeline__group">
                <summary>Show all {node.items.length} steps</summary>
                <ol>
                  {node.items.map((t) => (
                    <li key={t.seq}>
                      <strong>{stateInfo(t.from_state).label} → {stateInfo(t.to_state).label}</strong>
                      {" "}<span className="muted">{stepText(t)}</span>
                      <Meta t={t} />
                    </li>
                  ))}
                </ol>
              </details>
            </li>
          );
        })}
      </ol>
    </>
  );
}
