import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link } from "react-router";

import { ApiError, api } from "../api/client";
import type { DecisionResult, OperationDetail, Proposal } from "../api/types";
import { Icon } from "../components/Icon";
import { approvedResult, comparesWithCurrent } from "../components/labels";
import { usePageTitle } from "../components/pageTitle";
import { Badge, ErrorState, Loading, StateCode, stateInfo, successWording } from "../components/States";
import { RelativeTime } from "../components/time";
import { useNow } from "../components/time";
import { CaseUnavailable, ChapterFooter, ChapterHeading } from "../components/WalkthroughLayout";
import { matchesStoryAction, matchingAttestation, useStoryAction, useStoryProposal, useWalkthroughCases } from "../demo/walkthrough";
import { useSession } from "../session/SessionProvider";

function EmailDecision({ proposal, operation }: { proposal: Proposal; operation: OperationDetail }) {
  const { principal, has } = useSession();
  const client = useQueryClient();
  const decisionKey = useRef(crypto.randomUUID());
  const submittedBody = useRef<Record<string, unknown> | null>(null);
  const [note, setNote] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const now = useNow(proposal.status === "open", 1000);
  const expired = Date.parse(proposal.expires_at) <= now;
  const changed = comparesWithCurrent(proposal.status) && (proposal.current.seq !== proposal.binding.seq
    || proposal.current.state !== proposal.binding.state || proposal.current.evidence_digest !== proposal.binding.evidence_digest);
  const own = proposal.proposed_by === principal?.principal_id || operation.requested_by === principal?.principal_id;
  const canDecide = has("reviewer") && proposal.status === "open" && !expired && !changed && !own;
  const decision = useMutation({
    mutationFn: (value: "approve" | "reject") => {
      submittedBody.current ??= { decision: value, binding_digest: proposal.binding_digest,
        decision_key: decisionKey.current, ...(note.trim() ? { note: note.trim() } : {}) };
      return api<DecisionResult>(`/proposals/${proposal.proposal_id}/decision`, {
        method: "POST", body: submittedBody.current,
      });
    },
    onSuccess: (result) => {
      setMessage(result.outcome === "rejected" ? "Proposal rejected. The email outcome has not been changed."
        : result.outcome === "replayed" ? "This decision was already recorded; showing the current result." : "Attestation approved. The current email outcome is shown below.");
      client.setQueryData(["proposals", "detail", proposal.proposal_id], result.proposal);
      void client.invalidateQueries({ queryKey: ["action", operation.operation_id] });
    },
    onError: (error) => {
      if (error instanceof ApiError) {
        decisionKey.current = crypto.randomUUID();
        submittedBody.current = null;
      }
      setMessage(error instanceof ApiError && error.code === "stale_approval"
        ? "Approval refused: the proposal changed. Inspect the current evidence before an investigator proposes again."
        : error instanceof ApiError && error.code === "already_decided" ? "Another reviewer has already decided. The current record is being refreshed." : null);
      void client.invalidateQueries({ queryKey: ["proposals", "detail", proposal.proposal_id] });
      void client.invalidateQueries({ queryKey: ["action", operation.operation_id] });
    },
  });
  const uncertain = decision.isError && !(decision.error instanceof ApiError);
  const attested = operation.state === "SUCCEEDED" && operation.success_provenance === "human_attested";
  return <section className="panel decision-case" aria-labelledby="email-decision">
    <div className="panel__header"><h2 id="email-decision">The same email · Customer #3002</h2><Badge tone={proposal.status === "approved" ? "ok" : "info"} icon={proposal.status === "approved" ? "check" : "mail"}>{expired && proposal.status === "open" ? "Expired proposal" : `${proposal.status[0]!.toUpperCase()}${proposal.status.slice(1)} proposal`}</Badge></div>
    <p className="story-role-chain">MCP agent requested <span aria-hidden="true">→</span> Investigator proposed <span aria-hidden="true">→</span> Reviewer decides</p>
    <dl className="story-decision-facts"><dt>Proposed step</dt><dd>Confirm the email was delivered</dd><dt>Provider reference</dt><dd><code>{proposal.attested_reference}</code></dd><dt>Evidence source</dt><dd>Investigator's attestation</dd></dl>
    <div className="table-wrap"><table className="stack-table"><caption className="visually-hidden">Email evidence when proposed compared with now</caption>
      <thead><tr><td /><th scope="col">When proposed</th><th scope="col">Current</th></tr></thead>
      <tbody><tr><th scope="row">Evidence</th><td data-label="When proposed">Recorded evidence</td><td data-label="Current">{proposal.status === "approved" ? approvedResult(proposal) : changed ? "Changed since proposal" : comparesWithCurrent(proposal.status) ? "Unchanged" : "Not compared after the decision"}</td></tr></tbody>
    </table></div>
    <p className="story-callout info"><Icon name="info" />Approval records human attestation, not independent provider verification.</p>
    {message && <p className="notice info" role="status">{message}</p>}
    {decision.isError && !message && <ErrorState error={decision.error} />}
    {uncertain && <p className="notice warn">The decision response was lost. Retry the same decision to retrieve its recorded result.</p>}
    {canDecide ? <form className="story-decision-form" onSubmit={(event) => { event.preventDefault(); if (!decision.isPending) decision.mutate("approve"); }}>
      <label htmlFor="story-decision-note">Decision note (optional)</label>
      <input id="story-decision-note" value={note} onChange={(event) => setNote(event.target.value)} maxLength={2000} disabled={decision.isPending || uncertain} />
      <div className="row"><button type="submit" disabled={decision.isPending || (uncertain && decision.variables !== "approve")} aria-describedby="story-approve-meaning"><Icon name="check" />{decision.isPending && decision.variables === "approve" ? "Approving…" : uncertain && decision.variables === "approve" ? "Retry approval" : "Approve attestation"}</button>
        <button type="button" className="secondary" disabled={decision.isPending || (uncertain && decision.variables !== "reject")} onClick={() => decision.mutate("reject")}><Icon name="x" />{uncertain && decision.variables === "reject" ? "Retry rejection" : "Reject proposal"}</button></div>
      <p id="story-approve-meaning" className="hint">Records the applied-effect reference. SafeAct rechecks your authority and the proposal binding.</p>
    </form> : !proposal.decision && <p className="notice warn">{own ? "A different reviewer must decide on your request or proposal." : expired || proposal.status === "expired" ? "This proposal expired. Prepare the demo again to obtain a fresh proposal; no email will be resent." : "This proposal cannot be decided using the current evidence. An investigator can propose again."}</p>}
    <div className={`story-callout ${attested ? "ok" : "neutral"}`} role="status"><Icon name={attested ? "check" : "info"} /><span>{attested ? successWording(operation.success_provenance).title : proposal.status === "approved" ? "Approval recorded; waiting for the evidence-bound outcome." : "No approved delivery attestation is recorded here yet."} · {operation.attempts.length} recorded {operation.attempts.length === 1 ? "attempt" : "attempts"}</span></div>
    <p className="muted small">Local mock demo uses a synthetic provider reference. <Link to={`/actions/${operation.operation_id}?tab=evidence`}>Inspect the email evidence</Link></p>
  </section>;
}

export function DecisionChapter() {
  usePageTitle("Human decision");
  const cases = useWalkthroughCases();
  const email = useStoryAction(cases.data?.email.operation_id);
  const order = useStoryAction(cases.data?.order.operation_id);
  const emailProposal = useStoryProposal(cases.data?.email.proposal_id);
  const orderProposal = useStoryProposal(cases.data?.order.proposal_id);
  const retry = () => { void cases.refetch(); if (cases.data) for (const query of [email, order, emailProposal, orderProposal]) void query.refetch(); };
  if (cases.isError || [email, order, emailProposal, orderProposal].some((query) => query.isError)) return <CaseUnavailable retry={retry} />;
  if (!cases.data || !email.data || !order.data || !emailProposal.data || !orderProposal.data) return <Loading label="Loading the connected email and order proposals…" />;
  const e = email.data;
  const o = order.data;
  const ep = emailProposal.data;
  const p = orderProposal.data;
  if (!matchesStoryAction(e, "email") || !matchesStoryAction(o, "order") || !matchingAttestation(ep, e.operation_id) || !matchingAttestation(p, o.operation_id)) {
    return <CaseUnavailable retry={retry}>These proposals do not match the prepared email and order. No decision is offered.</CaseUnavailable>;
  }
  const changed = p.current.evidence_digest !== p.binding.evidence_digest || p.current.seq !== p.binding.seq;
  const bindingLabel = p.binding.state === "ESCALATED_UNRESOLVED" ? "Unresolved" : stateInfo(p.binding.state).label;
  const currentLabel = o.outcome_class === "unknown" ? "Unresolved" : stateInfo(o.state).label;
  const trace = cases.data.stale_refusal;
  return <div className="story-stack">
    <ChapterHeading title="A human decision, bound to the evidence." description="The investigator proposes a resolution for the agent's email. A separate reviewer decides." />
    <div className="story-grid story-grid--decisions">
      <EmailDecision key={ep.proposal_id} proposal={ep} operation={e} />
      <section className="panel decision-case" aria-labelledby="order-decision"><div className="panel__header"><h2 id="order-decision">The order · Case #2002</h2><Badge tone="warn" icon="alert">Earlier approval refused</Badge></div>
        <p>A provider observation changed the evidence after the investigator prepared this proposal. The captured server response below demonstrates the refusal.</p>
        <div className="table-wrap"><table className="stack-table"><caption className="visually-hidden">Order evidence when proposed compared with now</caption>
          <thead><tr><td /><th scope="col">When proposed</th><th scope="col">Current</th></tr></thead>
          <tbody><tr><th scope="row">Order outcome</th><td data-label="When proposed">{bindingLabel}<StateCode state={p.binding.state} label={bindingLabel} /></td><td data-label="Current">{currentLabel}<StateCode state={o.state} label={currentLabel} /></td></tr>
            <tr><th scope="row">Evidence</th><td data-label="When proposed">Earlier observation</td><td data-label="Current">{changed ? <span className="changed-flag"><Icon name="alert" />Changed</span> : "No change in current view"}</td></tr></tbody>
        </table></div>
        <p className="story-callout warn"><Icon name="lock" />An old approval cannot authorize a decision after the evidence changes.</p>
        <button type="button" className="secondary" disabled aria-describedby="stale-blocked">Approve attestation</button>
        <p id="stale-blocked" className="hint">This captured stale proposal is shown for inspection, not for a new approval.</p>
        <Link to={`/actions/${o.operation_id}?tab=evidence`}>Inspect current evidence →</Link>
        <div className="story-recording"><p>Recorded server response · <RelativeTime value={trace.captured_at} /></p><code>{trace.http_status} {trace.code}</code><p className="small">Reason: {trace.reason.replaceAll("_", " ")}. This is captured demo output, not a new approval attempt from this page.</p></div>
        <p className="story-callout warn"><Icon name="alert" /><strong>{o.outcome_class === "unknown" ? "Approval refused. The order remains unresolved." : "Approval was refused. Inspect the current outcome separately."}</strong></p>
      </section>
    </div>
    <ChapterFooter index={3} />
  </div>;
}
