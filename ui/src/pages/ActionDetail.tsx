import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router";

import { ApiError, api } from "../api/client";
import { forgetRequestKey } from "../api/requestKey";
import type { CancelResult, OperationDetail, Proposal, ProposalAction } from "../api/types";
import { Actor } from "../components/Actor";
import { AttemptCard, Raw } from "../components/EvidenceCard";
import { Icon } from "../components/Icon";
import {
  actionIcon, actionLabel, connectorLabel, evidenceSummary, humanize, latestRefundStatus, operationName,
  refundStatusLabel,
} from "../components/labels";
import { EscalationFollowUp, NextCheck, NextCheckLine } from "../components/NextCheck";
import { usePageTitle } from "../components/pageTitle";
import { Badge, Empty, ErrorState, Loading, StateBadge, successWording } from "../components/States";
import { Stepper } from "../components/Stepper";
import { Tabs } from "../components/Tabs";
import { RelativeTime } from "../components/time";
import { Timeline, stepText } from "../components/Timeline";
import { useSession } from "../session/SessionProvider";

const OUTCOME = {
  confirmed: {
    tone: "ok", title: "Confirmed",
    text: "Evidence settles what happened. Nothing more will be sent.",
  },
  unknown: {
    tone: "warn", title: "Outcome unknown",
    text: "The provider may or may not have applied this. SafeAct will not repeat it unless the "
      + "connector contract makes a retry safe; a reviewer may need to look.",
  },
  in_progress: { tone: "info", title: "In progress", text: "Being processed." },
  cancelled: {
    tone: "info", title: "Cancelled",
    text: "Cancelled before any attempt was made: nothing was sent.",
  },
} as const;

export function ActionDetail() {
  const { operationId = "" } = useParams();
  const location = useLocation();
  const { principal } = useSession();
  const arrival = location.state as { submittedKey?: string; notice?: string } | null;
  const detail = useQuery({
    queryKey: ["action", operationId],
    queryFn: ({ signal }) => api<OperationDetail>(`/actions/${operationId}`, { signal }),
  });
  // Only release retained intent after its matching server record is visible.
  useEffect(() => {
    if (detail.data && principal) {
      forgetRequestKey(detail.data.request_key, `${principal.tenant_id}.${principal.principal_id}`);
    }
  }, [detail.data, principal]);
  usePageTitle(detail.data ? operationName(detail.data) : "Action");

  if (detail.isPending) return <div className="page page--detail"><Loading label="Loading action…" /></div>;
  if (detail.isError) {
    return <div className="page page--detail"><ErrorState error={detail.error} retry={() => detail.refetch()} /></div>;
  }
  const op = detail.data;
  const outcome = OUTCOME[op.outcome_class];
  // A success is worded by how it was established, never as provider proof by default.
  const headline = op.state === "SUCCEEDED" ? successWording(op.success_provenance) : outcome;
  const last = op.transitions.at(-1);
  const records = op.attempts.reduce((n, a) => n + a.outcomes.length, 0);
  const summary = evidenceSummary(op);
  const max = op.contract?.max_attempts ?? null;
  const settled = last && ["FAILED_CONFIRMED", "CANCELLED_BEFORE_CLAIM", "ESCALATED_UNRESOLVED"].includes(op.state);

  const overview = (
    <>
      <section className="panel" aria-labelledby="happened-title">
        <h2 id="happened-title" className="section-title">What happened</h2>
        <dl className="facts">
          <dt>Request</dt><dd>{operationName(op)}</dd>
          <dt>Requested by</dt><dd><Actor id={op.requested_by} principal={principal} /></dd>
          {last && !settled && (
            <><dt>Latest step</dt><dd>{stepText(last, op.success_provenance)} <span className="muted small">(<RelativeTime value={last.created_at} />)</span></dd></>
          )}
          <dt>What happens next</dt><dd><NextCheckLine check={op.next_check} /></dd>
          <dt>Attempts</dt>
          <dd>
            {op.attempts.length === 0 ? "None yet: nothing has been sent."
              : `${op.attempts.length}${max ? ` of at most ${max}` : ""}`}
          </dd>
          <dt>Evidence</dt>
          <dd className="stack-sm">
            <span>{records === 0 ? "No evidence recorded yet." : <>
              {summary.text}
              {(summary.split || summary.latest) && <span className="muted"> ({summary.split}{summary.split && summary.latest && "; "}
                {summary.latest && <>latest <RelativeTime value={summary.latest} /></>})</span>}
            </>}</span>
            {op.success_provenance === "human_attested" && (
              <Badge tone="warn" icon="user">Success attested by a reviewer, not verified by the provider</Badge>
            )}
            {op.success_provenance === "provider_verified" && (
              <Badge tone="ok" icon="check">Verified with the provider</Badge>
            )}
          </dd>
          {op.connector_id === "stripe-test" && op.state === "SUCCEEDED" && <StripeRefundFacts op={op} />}
          {op.workflow && (
            <><dt>Part of</dt><dd><Link to={`/workflows/${op.workflow.workflow_id}`}>A replacement for a customer</Link>
              {" "}<span className="muted">({WORKFLOW_STEP[op.workflow.step] ?? humanize(op.workflow.step)} step)</span></dd></>
          )}
        </dl>
      </section>
      <ActionControls op={op} />
    </>
  );

  const timeline = (
    <section className="panel" aria-labelledby="timeline-title">
      <h2 id="timeline-title" className="section-title">Timeline</h2>
      <Timeline transitions={op.transitions} recordedAt={op.created_at} provenance={op.success_provenance} />
    </section>
  );

  const evidence = (
    <section aria-labelledby="attempts-title">
      <h2 id="attempts-title" className="section-title">Attempts and evidence</h2>
      {records > 0 && <p className="muted small">{records} {records === 1 ? "record" : "records"} in total. {summary.text}.</p>}
      <p className="hint">
        "Not found" or "inconclusive" never proves an action failed: only provider-confirmed
        rejection does.
      </p>
      <div className="attempts">
        {op.attempts.length === 0
          ? <Empty>No attempt has been made yet.<span className="empty__hint muted"> Nothing has been sent to the provider.</span></Empty>
          : op.attempts.map((attempt) => <AttemptCard key={attempt.attempt_id} attempt={attempt} max={max} />)}
      </div>
    </section>
  );

  const technical = (
    <>
      <section className="panel" aria-labelledby="facts-title">
        <h2 id="facts-title" className="section-title">Request</h2>
        <dl className="facts">
          <dt>Operation id</dt><dd><code>{op.operation_id}</code></dd>
          <dt>Action type</dt><dd><code>{op.action_type}</code></dd>
          <dt>Resource</dt><dd><code>{op.resource_id}</code></dd>
          <dt>Payload</dt><dd><Raw value={op.payload} /></dd>
          <dt>Payload SHA-256</dt><dd><code>{op.payload_sha256}</code></dd>
          <dt>Request key</dt><dd><code>{op.request_key}</code></dd>
          <dt>Connector</dt><dd><code>{op.connector_id}</code> (contract {op.contract_version})</dd>
          <dt>Provider account</dt><dd><code>{op.provider_account_id}</code></dd>
          <dt>Provider idempotency key</dt>
          <dd>{op.provider_key_protected ? "Yes: a retry reuses the same key" : "None (this connector has no native key)"}</dd>
          <dt>Requested by</dt><dd><code>{op.requested_by ?? "—"}</code></dd>
          <dt>State</dt><dd><code>{op.state}</code>, version {op.seq}</dd>
          <dt>Recorded</dt><dd><time dateTime={op.created_at}>{new Date(op.created_at).toLocaleString()}</time></dd>
          {op.workflow && <><dt>Workflow</dt><dd><code>{op.workflow.workflow_id}</code> · step <code>{op.workflow.step}</code></dd></>}
          <NextCheck check={op.next_check} />
        </dl>
      </section>

      {op.contract && (
        <section className="panel" aria-labelledby="contract-title">
          <h2 id="contract-title" className="section-title">What the connector contract assumes</h2>
          <dl className="facts">
            <dt>Side effect</dt><dd>{humanize(op.contract.side_effect_class)}</dd>
            <dt>Native idempotency</dt>
            <dd>{op.contract.native_idempotency
              ? `Yes, key kept ${op.contract.key_retention_seconds} s`
              : "No"}</dd>
            <dt>Lookup</dt><dd>{humanize(op.contract.lookup_semantics)}</dd>
            <dt>Request deadline</dt><dd>{op.contract.request_deadline_seconds} s</dd>
            <dt>Investigation window</dt><dd>{op.contract.investigation_window_seconds} s</dd>
            <dt>Attempts allowed</dt><dd>{op.contract.max_attempts}</dd>
            <dt>Reopens allowed</dt><dd>{op.contract.max_reopens}</dd>
            <dt>Reference</dt><dd>{op.contract.contract_reference}</dd>
          </dl>
        </section>
      )}

      {op.cancellation_requests.length > 0 && (
        <section className="panel" aria-labelledby="cancel-title">
          <h2 id="cancel-title" className="section-title">Cancellation requests</h2>
          <ul className="evidence">
            {op.cancellation_requests.map((c, i) => (
              <li key={i} className="stack-sm">
                <span className="muted small">
                  <time dateTime={c.created_at}>{new Date(c.created_at).toLocaleString()}</time> by <code>{c.requested_by}</code>
                </span>
                <Raw value={c.details} />
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );

  return (
    <div className="page page--detail">
      <Link to="/actions" className="back-link"><Icon name="arrowLeft" />All actions</Link>
      <div className="stack">
        {arrival?.notice && <p className="notice ok" role="status">{arrival.notice}</p>}
        <section className="panel hero" aria-labelledby="action-title">
          <div className="hero__top">
            <div className="hero__title">
              <span className="icon-circle icon-circle--lg info"><Icon name={actionIcon(op.action_type)} /></span>
              <div>
                <h1 id="action-title">{operationName(op)}</h1>
                <p className="hero__sub">
                  <span>{actionLabel(op.action_type)}</span>
                  <span>{connectorLabel(op.connector_id)}</span>
                  <span>Recorded <RelativeTime value={op.created_at} /></span>
                </p>
              </div>
            </div>
            <StateBadge state={op.state} />
          </div>
          <div className="hero__attribution"><span className="muted">Requested by</span><Actor id={op.requested_by} principal={principal} /></div>
          <p className="hero__outcome">
            {/* A settled action says why in its own words; otherwise the outcome's general meaning. */}
            <strong>{headline.title}.</strong> {settled ? <span>{stepText(last)}</span> : headline.text}
            {op.state === "ESCALATED_UNRESOLVED" && <EscalationFollowUp check={op.next_check} />}
          </p>
          <Stepper state={op.state} transitions={op.transitions} provenance={op.success_provenance}
                   nextCheck={op.next_check?.kind} />
        </section>
        <Tabs label="Action details" tabs={[
          { id: "overview", label: "Overview", content: overview },
          { id: "timeline", label: "Timeline", content: timeline },
          { id: "evidence", label: "Evidence", content: evidence },
          { id: "technical", label: "Technical details", content: technical },
        ]} />
        <p className="context-note"><Icon name="robot" />AI agents can request and investigate. Restricted recovery needs a separate human reviewer.</p>
      </div>
    </div>
  );
}

const WORKFLOW_STEP: Record<string, string> = {
  charge: "shipping charge", order: "replacement order", compensation: "refund",
};

/** What "Succeeded" means for a Stripe refund, and what it does not. */
function StripeRefundFacts({ op }: { op: OperationDetail }) {
  const status = latestRefundStatus(op.attempts);
  return (
    <>
      <dt>At Stripe</dt>
      <dd className="stack-sm">
        <span>Stripe accepted the request and created the refund.</span>
        <span>
          Refund status Stripe reported at the time:{" "}
          <strong>{status === null ? "Not recorded" : refundStatusLabel(status)}</strong>
          <span className="muted"> (not a live value)</span>
        </span>
        <span className="muted small">Settlement to the customer is out of scope: SafeAct does not track it.</span>
      </dd>
    </>
  );
}

function ActionControls({ op }: { op: OperationDetail }) {
  const { has } = useSession();
  const client = useQueryClient();
  const navigate = useNavigate();
  const [message, setMessage] = useState<{ tone: string; text: string } | null>(null);

  const refresh = () => client.invalidateQueries({ queryKey: ["action", op.operation_id] });

  const cancel = useMutation({
    mutationFn: () => api<CancelResult>(`/actions/${op.operation_id}/cancel`, { method: "POST" }),
    onSuccess: (result) => {
      setMessage(result.outcome === "cancelled"
        ? { tone: "ok", text: "Cancelled. Nothing was sent." }
        : {
          tone: "warn",
          text: result.outcome === "refused_may_have_dispatched"
            ? "Not cancelled: it may already have been sent. Nothing was undone."
            : "Not cancelled: this action is already finished or part of a workflow.",
        });
      void refresh();
    },
    onError: (error) => setMessage({ tone: "bad", text: (error as Error).message }),
  });

  const verify = useMutation({
    mutationFn: () => api(`/actions/${op.operation_id}/verification-requests`, { method: "POST" }),
    onSuccess: () => setMessage({ tone: "ok", text: "Verification queued: one more provider lookup will run." }),
    onError: (error) => setMessage({
      tone: "warn",
      text: error instanceof ApiError && error.code === "not_eligible"
        ? "A lookup cannot run now (the contract limits how often). Propose REVERIFY for a reviewer instead."
        : (error as Error).message,
    }),
  });

  const canCancel = !op.workflow && op.state === "RECORDED" && (has("requester") || has("investigator"));
  const investigator = has("investigator");
  if (!canCancel && !investigator) return null;

  return (
    <section className="panel stack" aria-labelledby="controls-title">
      <h2 id="controls-title" className="section-title">What you can do</h2>
      {message && <p className={`notice ${message.tone}`} role="status">{message.text}</p>}
      <div className="row">
        {canCancel && (
          <button type="button" className="danger" disabled={cancel.isPending}
                  onClick={() => cancel.mutate()}>
            {cancel.isPending ? "Cancelling…" : "Request cancellation"}
          </button>
        )}
        {investigator && op.state === "ESCALATED_UNRESOLVED" && op.contract?.lookup_semantics !== "unavailable" && (
          <button type="button" className="secondary" disabled={verify.isPending}
                  onClick={() => verify.mutate()}>
            Request a verification lookup
          </button>
        )}
      </div>
      {investigator && op.contract?.lookup_semantics === "unavailable" && <p className="context-note"><Icon name="info" />This provider has no lookup. An uncertain send needs human evidence; another send is not offered here.</p>}
      {investigator && ["ESCALATED_UNRESOLVED", "VERIFYING"].includes(op.state) && (
        <ProposeForm op={op} onProposed={(p) => navigate(`/proposals/${p.proposal_id}`)} />
      )}
    </section>
  );
}

function ProposeForm({ op, onProposed }: { op: OperationDetail; onProposed: (p: Proposal) => void }) {
  const actions: ProposalAction[] = op.state === "VERIFYING"
    ? ["PROTECTED_RETRY"] : op.contract?.lookup_semantics === "unavailable" ? ["ATTEST_APPLIED"] : ["ATTEST_APPLIED", "REVERIFY"];
  const [action, setAction] = useState<ProposalAction>(actions[0]!);
  const [rationale, setRationale] = useState("");
  const [reference, setReference] = useState("");
  const [touched, setTouched] = useState(false);

  const propose = useMutation({
    mutationFn: () => api<Proposal>(`/actions/${op.operation_id}/proposals`, {
      method: "POST",
      body: {
        action, rationale,
        ...(action === "ATTEST_APPLIED" ? { attested_reference: reference.trim() } : {}),
      },
    }),
    onSuccess: onProposed,
  });

  const missing = !rationale.trim() || (action === "ATTEST_APPLIED" && !reference.trim());
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setTouched(true);
    if (!missing && !propose.isPending) propose.mutate();
  };

  return (
    <form onSubmit={submit} noValidate aria-labelledby="propose-title">
      <h3 id="propose-title">Propose a next action for a reviewer</h3>
      <p className="hint form-intro">A reviewer who is neither you nor the requester decides. You cannot approve it yourself.</p>
      <div className="field">
        <label htmlFor="propose-action">Action</label>
        <select id="propose-action" value={action} onChange={(e) => setAction(e.target.value as ProposalAction)}>
          {actions.map((a) => <option key={a} value={a}>{ACTION_LABEL[a]}</option>)}
        </select>
      </div>
      {action === "ATTEST_APPLIED" && (
        <div className="field">
          <label htmlFor="propose-reference">
            Provider reference for the applied effect
            <span className="hint">Only for an effect you can see was applied. A person can never confirm that something did not happen.</span>
          </label>
          <input id="propose-reference" value={reference} onChange={(e) => setReference(e.target.value)}
                 maxLength={255} aria-describedby={touched && !reference.trim() ? "propose-reference-error" : undefined}
                 placeholder="For example msg_123 or re_123"
                 aria-invalid={touched && !reference.trim()} />
          {touched && !reference.trim() && <p id="propose-reference-error" className="field-error"><Icon name="alert" />Enter the provider's reference.</p>}
        </div>
      )}
      <div className="field">
        <label htmlFor="propose-rationale">Why</label>
        <textarea id="propose-rationale" value={rationale} onChange={(e) => setRationale(e.target.value)}
                  maxLength={2000} aria-describedby={touched && !rationale.trim() ? "propose-rationale-error" : undefined}
                  placeholder="The evidence you reviewed and why this next step is safe"
                  aria-invalid={touched && !rationale.trim()} />
        {touched && !rationale.trim() && <p id="propose-rationale-error" className="field-error"><Icon name="alert" />Explain the evidence you reviewed.</p>}
      </div>
      {propose.isError && <ErrorState error={propose.error} />}
      <button type="submit" disabled={propose.isPending}>
        {propose.isPending ? "Proposing…" : "Propose"}
      </button>
    </form>
  );
}

export const ACTION_LABEL: Record<ProposalAction, string> = {
  ATTEST_APPLIED: "Attest the effect was applied",
  REVERIFY: "Re-verify beyond the contract's lookup limits",
  PROTECTED_RETRY: "Approve a protected retry",
};
