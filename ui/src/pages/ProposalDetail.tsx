import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link, useParams } from "react-router";

import { ApiError, api } from "../api/client";
import type { DecisionResult, OperationDetail, Proposal } from "../api/types";
import { Actor } from "../components/Actor";
import { Icon } from "../components/Icon";
import { approvedResult, comparesWithCurrent, operationName, proposalTitle } from "../components/labels";
import { usePageTitle } from "../components/pageTitle";
import { personName } from "../components/people";
import { ErrorState, Loading, StateBadge } from "../components/States";
import { TechnicalDetails } from "../components/TechnicalDetails";
import { RelativeTime } from "../components/time";
import { useSession } from "../session/SessionProvider";
import { ACTION_LABEL } from "./ActionDetail";
import { ProposalStatusBadge } from "./Proposals";

const STALE_REASON: Record<string, string> = {
  operation_changed: "the operation moved to a new state",
  evidence_changed: "new evidence was recorded",
  binding_changed: "what this proposal is bound to changed (for example a review policy)",
  client_out_of_date: "your screen showed an older version",
  expired: "the proposal expired",
};

const MEANING = {
  ATTEST_APPLIED: "Records the reviewer's reference as evidence that the effect was applied. "
    + "The success will be shown as reviewer-attested, never as provider-verified.",
  REVERIFY: "Allows one more provider lookup beyond the contract's normal limits. "
    + "It reopens the operation only if the provider gives a decisive answer.",
  PROTECTED_RETRY: "Allows the retry the contract marks as needing review. Every other "
    + "safety rule still applies.",
} as const;

const CONSEQUENCE = {
  ATTEST_APPLIED: "Marks it succeeded, shown as reviewer-attested.",
  REVERIFY: "Runs one more provider lookup.",
  PROTECTED_RETRY: "Lets the protected retry run.",
} as const;

export function ProposalDetail() {
  const { proposalId = "" } = useParams();
  const client = useQueryClient();
  const { has, principal } = useSession();
  const proposal = useQuery({
    queryKey: ["proposals", "detail", proposalId],
    queryFn: ({ signal }) => api<Proposal>(`/proposals/${proposalId}`, { signal }),
  });
  const operationId = proposal.data?.operation_id;
  const operation = useQuery({
    queryKey: ["action", operationId],
    queryFn: ({ signal }) => api<OperationDetail>(`/actions/${operationId}`, { signal }),
    enabled: Boolean(operationId),
  });
  usePageTitle(proposal.data
    ? proposalTitle(proposal.data.action, operation.data) ?? ACTION_LABEL[proposal.data.action] : "Review");

  // One key per decision on this screen: a repeated click, or a retry
  // after a lost response, returns the stored decision, never a second.
  const decisionKey = useRef<string>(crypto.randomUUID());
  const [note, setNote] = useState("");
  const [result, setResult] = useState<{ tone: string; text: string } | null>(null);

  const decide = useMutation({
    mutationFn: (decision: "approve" | "reject") => api<DecisionResult>(
      `/proposals/${proposalId}/decision`,
      {
        method: "POST",
        body: {
          decision,
          binding_digest: proposal.data!.binding_digest,
          decision_key: decisionKey.current,
          ...(note.trim() ? { note: note.trim() } : {}),
        },
      },
    ),
    onSuccess: (outcome) => {
      setResult({
        tone: "ok",
        text: outcome.outcome === "rejected" ? "Rejected. Nothing changed."
          : outcome.outcome === "replayed" ? "Already decided by you; showing that decision."
          : "Approved.",
      });
      client.setQueryData(["proposals", "detail", proposalId], outcome.proposal);
      void client.invalidateQueries({ queryKey: ["action", operationId] });
    },
    onError: (error) => {
      if (error instanceof ApiError && error.code === "stale_approval") {
        setResult({
          tone: "warn",
          text: `Not approved: ${STALE_REASON[error.reason ?? ""] ?? "it changed"} since this was shown. `
            + "Review the current evidence below; an investigator can propose again.",
        });
        void proposal.refetch();
        void operation.refetch();
      } else if (error instanceof ApiError && error.code === "already_decided") {
        setResult({ tone: "warn", text: "Another reviewer already decided this proposal." });
        void proposal.refetch();
      } else if (error instanceof ApiError && error.code === "self_review") {
        setResult({ tone: "bad", text: "You cannot decide on a proposal you made or an action you requested." });
      } else {
        setResult({ tone: "bad", text: (error as Error).message });
      }
      // A definitive refusal ends this decision; a lost response may not
      // have: retrying then reuses the key, so the server replays it.
      if (error instanceof ApiError) decisionKey.current = crypto.randomUUID();
    },
  });

  if (proposal.isPending) return <div className="page page--detail"><Loading label="Loading proposal…" /></div>;
  if (proposal.isError) {
    return <div className="page page--detail"><ErrorState error={proposal.error} retry={() => proposal.refetch()} /></div>;
  }
  const p = proposal.data;
  const versionChanged = p.current.seq !== p.binding.seq;
  const stateChanged = p.current.state !== p.binding.state;
  const evidenceChanged = p.current.evidence_digest !== p.binding.evidence_digest;
  const changed = versionChanged || evidenceChanged;
  // Changes are flagged only while the proposal can be decided or was refused as stale.
  const comparing = comparesWithCurrent(p.status);
  const op = operation.data;
  const canDecide = has("reviewer") && p.status === "open"
    && !changed && op && p.proposed_by !== principal?.principal_id
    && op.requested_by !== principal?.principal_id;
  const flag = <span className="changed-flag"><Icon name="alert" />Changed</span>;

  const title = proposalTitle(p.action, op) ?? ACTION_LABEL[p.action];

  return (
    <div className="page page--detail">
      <Link to="/proposals" className="back-link"><Icon name="arrowLeft" />All reviews</Link>
      <div className="page-header">
        <div>
          <h1>{title}</h1>
          <p className="page-header__sub">
            <ProposalStatusBadge status={p.status} />
            <span>Proposed by {personName(p.proposed_by, principal)}</span>
            {p.status === "open" && <><span aria-hidden="true">·</span><span>Expires <RelativeTime value={p.expires_at} /></span></>}
          </p>
        </div>
      </div>

      <div className="stack">
        {result && <p className={`notice ${result.tone}`} role="status">{result.text}</p>}
        {p.status === "stale" && !result && (
          <section className="notice warn" role="status" aria-labelledby="stale-title">
            <p className="notice__title" id="stale-title"><Icon name="alert" />This proposal is stale. It cannot be approved.</p>
            <p>
              Since it was made, {evidenceChanged ? "new evidence was recorded"
                : changed ? "the operation moved to a new state"
                : "it is no longer a permitted next action"}.
              {" "}Review the current evidence below; an investigator can propose again.
            </p>
          </section>
        )}

        {p.status === "expired" && !result && (
          <section className="notice neutral" role="status" aria-labelledby="expired-title">
            <p className="notice__title" id="expired-title"><Icon name="minus" />This proposal expired before a decision.</p>
            <p>It can no longer be approved. An investigator can propose again if it is still needed.</p>
          </section>
        )}

        <section className="panel" aria-labelledby="proposal-facts">
          <h2 id="proposal-facts" className="section-title">The proposal</h2>
          <dl className="facts">
            <dt>Proposed by</dt><dd><Actor id={p.proposed_by} principal={principal} /></dd>
            {op && <><dt>Original requester</dt><dd><Actor id={op.requested_by} principal={principal} /></dd></>}
            <dt>Proposed step</dt><dd><strong>{ACTION_LABEL[p.action]}.</strong> {MEANING[p.action]}</dd>
            <dt>Why</dt><dd>{p.rationale}</dd>
            {p.attested_reference && <><dt>Provider reference they saw</dt><dd><code>{p.attested_reference}</code></dd></>}
            <dt>Action</dt>
            <dd className="stack-sm">
              <Link to={`/actions/${p.operation_id}`}>{op ? operationName(op) : "Open the action"}</Link>
              {op && (
                <span className="muted small">
                  {op.attempts.length} {op.attempts.length === 1 ? "attempt" : "attempts"},{" "}
                  {op.attempts.reduce((n, a) => n + a.outcomes.length, 0)} evidence record(s).{" "}
                  <Link to={`/actions/${op.operation_id}?tab=evidence`}>Inspect the evidence</Link>
                </span>
              )}
            </dd>
            {p.decision && (
              <>
                <dt>Decision</dt>
                <dd>{p.decision.decision === "approve" ? "Approved" : "Rejected"} by {personName(p.decision.decided_by, principal)}
                  {" "}<RelativeTime value={p.decision.created_at} />
                  {p.decision.note && <>: {p.decision.note}</>}
                </dd>
              </>
            )}
          </dl>
        </section>

        <section className="panel" aria-labelledby="compare-title">
          <h2 id="compare-title" className="section-title">What you saw vs current</h2>
          <div className="table-wrap">
            <table className="compare">
              <caption className="visually-hidden">The action when the proposal was made, and now</caption>
              <thead>
                <tr>
                  <td />
                  <th scope="col">What you saw (when proposed)</th>
                  <th scope="col">Current</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <th scope="row">Status</th>
                  <td><StateBadge state={p.binding.state} /></td>
                  <td className={comparing && stateChanged ? "changed" : undefined}><StateBadge state={p.current.state} />{comparing && stateChanged && flag}</td>
                </tr>
                <tr>
                  <th scope="row">Evidence</th>
                  <td>As reviewed by the investigator</td>
                  <td className={comparing && evidenceChanged ? "changed" : undefined}>
                    {p.status === "approved" ? approvedResult(p)
                      : !comparing ? "Not compared after the decision"
                      : evidenceChanged ? "New evidence recorded since" : "No new evidence"}
                    {comparing && evidenceChanged && flag}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          {comparing && !changed && <p className="context-note"><Icon name="check" />Nothing has changed since the proposal was made.</p>}
          {comparing && versionChanged && !stateChanged && !evidenceChanged && (
            <p className="hint">The action's record was updated since; see Technical details.</p>
          )}
        </section>

        <TechnicalDetails>
          <dl className="facts">
            <dt>Proposal</dt><dd><code>{p.proposal_id}</code> · <code>{p.action}</code></dd>
            <dt>Operation id</dt><dd><code>{p.operation_id}</code></dd>
            <dt>Version</dt><dd>{p.binding.seq} when proposed, {p.current.seq} now</dd>
            <dt>Evidence digest</dt>
            <dd className="stack-sm">
              <span>when proposed <code>{p.binding.evidence_digest}</code></span>
              <span>now <code>{p.current.evidence_digest}</code></span>
            </dd>
            <dt>Payload SHA-256</dt><dd><code>{p.binding.payload_sha256}</code></dd>
            <dt>Contract</dt><dd><code>{p.binding.connector_id}</code> {p.binding.contract_version}</dd>
            <dt>Binding digest</dt><dd><code>{p.binding_digest}</code></dd>
            {p.current.eligibility && <><dt>Eligibility</dt><dd><code>{p.current.eligibility}</code></dd></>}
            <dt>Proposed by</dt><dd><code>{p.proposed_by}</code></dd>
            {p.decision && <><dt>Decided by</dt><dd><code>{p.decision.decided_by}</code></dd></>}
            <dt>Created</dt><dd><time dateTime={p.created_at}>{new Date(p.created_at).toLocaleString()}</time></dd>
            <dt>Expires</dt><dd><time dateTime={p.expires_at}>{new Date(p.expires_at).toLocaleString()}</time></dd>
          </dl>
        </TechnicalDetails>

        {operation.isError && <p className="notice warn" role="status">The action evidence could not be loaded. A decision is unavailable until it can be inspected. <button type="button" className="secondary" onClick={() => void operation.refetch()}>Retry loading evidence</button></p>}
        {has("reviewer") && op?.requested_by === principal?.principal_id && <p className="notice info">You requested this action. A different human reviewer must decide this proposal.</p>}
        {changed && p.status === "open" && <p className="notice warn">The evidence or action version changed. Inspect the current evidence; an investigator can propose again.</p>}
        {(p.status === "stale" || changed) && <Link className="button secondary" to={`/actions/${p.operation_id}?tab=evidence`}>Inspect current evidence</Link>}

        {canDecide && (
          <section className="panel stack" aria-labelledby="decide-title">
            <h2 id="decide-title" className="section-title">Decide</h2>
            <div className="field">
              <label htmlFor="decision-note">Note <span className="hint">Optional</span></label>
              <textarea id="decision-note" value={note} onChange={(e) => setNote(e.target.value)}
                        placeholder="Anything the next person should know" />
            </div>
            <div className="decide">
              <div className="decide__option">
                <button type="button" disabled={decide.isPending} aria-describedby="approve-means"
                        onClick={() => decide.mutate("approve")}>
                  <Icon name="check" />
                  {decide.isPending && decide.variables === "approve" ? "Approving…" : "Approve"}
                </button>
                <p id="approve-means" className="hint">{CONSEQUENCE[p.action]}</p>
              </div>
              <div className="decide__option">
                <button type="button" className="danger" disabled={decide.isPending} aria-describedby="reject-means"
                        onClick={() => decide.mutate("reject")}>
                  <Icon name="x" />Reject
                </button>
                <p id="reject-means" className="hint">Nothing changes; the operation stays as it is.</p>
              </div>
            </div>
          </section>
        )}
      </div>
    </div>
  );
}
