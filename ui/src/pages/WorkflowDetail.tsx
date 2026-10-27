import { useQueries, useQuery } from "@tanstack/react-query";
import { Fragment, useEffect } from "react";
import { Link, useParams } from "react-router";

import { api } from "../api/client";
import type { OperationDetail, WorkflowDetail as Workflow } from "../api/types";
import { forgetWorkflow } from "../api/workflowRequest";
import { groupEvidence } from "../components/EvidenceCard";
import { Icon } from "../components/Icon";
import { actionLabel, humanize, money, resourceLabel } from "../components/labels";
import type { IconName } from "../components/Icon";
import { usePageTitle } from "../components/pageTitle";
import { personName } from "../components/people";
import { ErrorState, Loading, StateBadge, stateInfo } from "../components/States";
import { TechnicalDetails } from "../components/TechnicalDetails";
import { stepText } from "../components/Timeline";
import { RelativeTime } from "../components/time";
import { useSession } from "../session/SessionProvider";

const STATUS: Record<string, { title: string; tone: string; meaning: string; next: string }> = {
  IN_PROGRESS: { title: "Replacement in progress", tone: "info",
    meaning: "The shipping charge and replacement order are processed in sequence.",
    next: "Follow the active step below. An uncertain response is investigated before another action is allowed." },
  COMPLETED: { title: "Replacement completed", tone: "ok",
    meaning: "Recorded evidence confirms the shipping charge and replacement order.",
    next: "Open each step to inspect its evidence and whether it was provider verified or reviewer attested." },
  FAILED_NO_EFFECT: { title: "Shipping charge not applied", tone: "info",
    meaning: "Evidence confirms the charge was not applied. No replacement order was started.",
    next: "Review the charge rejection before deciding whether to request a new replacement." },
  COMPENSATING: { title: "Refund in progress", tone: "warn",
    meaning: "The charge succeeded and the order is confirmed not created. A separate refund is being processed.",
    next: "Follow the refund step. A refund request alone does not confirm that the customer received it." },
  COMPENSATED_VERIFIED: { title: "Refund confirmed", tone: "ok",
    meaning: "Evidence confirms a refund of the shipping charge after the order failed. The replacement was not completed.",
    next: "Inspect the refund evidence and its source below. The original charge and separate refund remain in the history." },
  COMPENSATION_UNRESOLVED: { title: "Refund needs investigation", tone: "warn",
    meaning: "The replacement order failed, and the shipping fee has not been confirmed refunded.",
    next: "An investigator should inspect the refund evidence. Do not issue another refund while its outcome is uncertain." },
  UNRESOLVED: { title: "Replacement needs investigation", tone: "warn",
    meaning: "A step remains uncertain. An order with an unknown outcome might already exist, so a refund is withheld.",
    next: "An investigator should open the unresolved step and inspect provider evidence before proposing a next action." },
};
const STEPS: { key: string; label: string; icon: IconName; absent: string }[] = [
  { key: "charge", label: "1. Shipping charge", icon: "card", absent: "Not started." },
  { key: "order", label: "2. Replacement order", icon: "box", absent: "Starts only after the charge succeeds." },
  { key: "compensation", label: "Refund if the order fails", icon: "refund",
    absent: "Created only after a successful charge and confirmed order failure." },
];

const STEP_NAMES: Record<string, string> = {
  charge: "Shipping charge", order: "Replacement order", compensation: "Refund",
};

function stepName(op: OperationDetail) {
  return op.workflow ? STEP_NAMES[op.workflow.step] ?? op.workflow.step : actionLabel(op.action_type);
}

// Lookups and pick-ups are kept in each step's own timeline; this one
// keeps what a person needs to follow the replacement.
const QUIET = new Set(["CLAIMED", "DISPATCHING", "VERIFYING"]);

export function WorkflowDetail() {
  const { principal } = useSession();
  const { workflowId = "" } = useParams();
  const workflow = useQuery({
    queryKey: ["workflow", workflowId],
    queryFn: ({ signal }) => api<Workflow>(`/workflows/${workflowId}`, { signal }),
    // Also catch a new step if the live channel is unavailable.
    refetchInterval: 5_000,
  });
  const steps = useQueries({ queries: (workflow.data?.steps ?? []).map((step) => ({
    queryKey: ["action", step.operation_id],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      api<OperationDetail>(`/actions/${step.operation_id}`, { signal }),
    refetchInterval: 5_000,
  })) });
  useEffect(() => {
    if (workflow.data && principal) forgetWorkflow(
      `${principal.tenant_id}.${principal.principal_id}`, workflow.data.request_key);
  }, [workflow.data, principal]);
  const caseLabel = workflow.data ? resourceLabel(workflow.data.plan.order.resource_id) : null;
  usePageTitle(caseLabel ? `Replacement for ${caseLabel}` : "Replacement");

  if (workflow.isPending) return <div className="page page--detail"><Loading label="Loading replacement workflow…" /></div>;
  if (workflow.isError) {
    return <div className="page page--detail"><ErrorState error={workflow.error} retry={() => workflow.refetch()} /></div>;
  }
  const w = workflow.data;
  const charge = steps[w.steps.findIndex((step) => step.step === "charge")]?.data;
  let sku = "Unavailable";
  try {
    const payload = JSON.parse(w.plan.order.payload_canonical) as Record<string, unknown>;
    if (typeof payload.sku === "string") sku = payload.sku;
  } catch { /* An unavailable plan detail cannot become a completion claim. */ }
  const fee = typeof charge?.payload.amount_minor === "number" ? money(charge.payload.amount_minor) : null;
  const customer = typeof charge?.payload.customer_id === "string" ? resourceLabel(charge.payload.customer_id) : null;
  const status = STATUS[w.status] ?? { title: "Workflow status unavailable", tone: "warn",
    meaning: "The workflow status is not recognized. No completion claim can be made.",
    next: "Refresh the workflow and ask an investigator to inspect its steps." };
  const timeline = steps.flatMap((query) => {
    if (!query.data) return [];
    const op = query.data;
    return [
      { id: `${op.operation_id}-recorded`, at: op.created_at, step: stepName(op),
        operationId: op.operation_id, text: "Recorded before anything was sent.", tone: "info", icon: "clock" as IconName },
      ...op.transitions.filter((t) => !QUIET.has(t.to_state)).map((t) => ({
        id: `${op.operation_id}-transition-${t.seq}`, at: t.created_at,
        step: stepName(op), operationId: op.operation_id,
        text: `${stateInfo(t.to_state).label}: ${stepText(t)}`,
        tone: stateInfo(t.to_state).tone, icon: stateInfo(t.to_state).icon })),
      ...op.attempts.flatMap((a) => groupEvidence(a.outcomes).map((group, i) => {
        const e = group[0]!;
        return {
          id: `${a.attempt_id}-evidence-${i}`, at: group.at(-1)!.created_at, step: stepName(op),
          operationId: op.operation_id,
          text: `Attempt ${a.ordinal}: ${humanize(e.evidence_kind ?? e.outcome)}`
            + (group.length > 1 ? ` (${group.length} checks)` : "")
            + (e.provenance === "human_attested" ? ", attested by a reviewer" : ", observed by the system"),
          tone: "neutral", icon: "dot" as IconName,
        };
      })),
    ];
  }).sort((a, b) => Date.parse(a.at) - Date.parse(b.at) || a.id.localeCompare(b.id));
  const references = steps.flatMap((query) => query.data?.attempts.flatMap((a) => a.outcomes
    .filter((e) => e.evidence_reference)
    .map((e) => ({ step: stepName(query.data!), reference: e.evidence_reference! }))) ?? []);

  return (
    <div className="page page--detail">
      <Link to="/actions" className="back-link"><Icon name="arrowLeft" />All actions</Link>
      <div className="page-header">
        <div>
          <h1>Replacement for {caseLabel}</h1>
          <p className="page-header__sub">
            <span>Requested by {personName(w.created_by, principal)}</span>
            <span aria-hidden="true">·</span>
            <span>Recorded <RelativeTime value={w.created_at} /></span>
          </p>
        </div>
      </div>
      <div className="stack">
      <section className={`notice ${status.tone}`} aria-labelledby="workflow-status">
        <h2 id="workflow-status">{status.title}</h2>
        <p>{status.meaning}</p>
        <p><strong>Next safe step:</strong> {status.next}</p>
      </section>
      <section aria-labelledby="workflow-steps">
        <h2 id="workflow-steps" className="section-title">Steps</h2>
        <div className="flow" aria-label="Charge, then order; refund only on confirmed order failure">
          {STEPS.map(({ key, label, icon, absent }) => {
            const stepIndex = w.steps.findIndex((s) => s.step === key);
            const step = w.steps[stepIndex];
            const detail = steps[stepIndex];
            const op = detail?.data;
            const info = step ? stateInfo(step.state) : null;
            return <section key={key} className={`panel flow__step${step ? "" : " absent"}`} aria-label={label}>
              <div className="flow__head">
                <span className={`icon-circle ${info?.tone ?? ""}`}><Icon name={icon} /></span>
                <h3 className="flow__name">{label}{key === "charge" && fee ? ` · ${fee}` : ""}</h3>
              </div>
              {step ? <>
                <StateBadge state={step.state} />
                <p className="small">{info!.meaning}</p>
                {op?.success_provenance === "human_attested" && <p className="hint">Success attested by a reviewer</p>}
                {op?.success_provenance === "provider_verified" && <p className="hint">Verified with the provider</p>}
                {detail?.isError && <p className="notice warn small">Evidence could not be loaded.
                  Open the step to retry; this does not prove failure.</p>}
                {step.compensates_operation_id && <p className="small">Refunds the <Link
                  to={`/actions/${step.compensates_operation_id}`}>original shipping charge</Link>.</p>}
                <Link to={`/actions/${step.operation_id}`}>Inspect {key === "compensation" ? "refund" : key} evidence</Link>
              </> : <p className="muted small">{absent}</p>}
            </section>;
          })}
        </div>
      </section>
      <section className="panel" aria-labelledby="workflow-request">
        <h2 id="workflow-request" className="section-title">Customer request</h2>
        <dl className="facts">
          <dt>Customer</dt><dd>{customer ?? "Loading or unavailable"}</dd>
          <dt>Support case</dt><dd>{caseLabel}</dd>
          <dt>Replacement item</dt><dd>{sku}</dd>
          <dt>Shipping fee</dt><dd>{fee ?? "Loading or unavailable"}</dd>
          <dt>Recorded</dt><dd><time dateTime={w.created_at}>{new Date(w.created_at).toLocaleString()}</time></dd>
        </dl>
      </section>
      <section aria-labelledby="workflow-timeline" className="panel">
        <h2 id="workflow-timeline" className="section-title">What happened</h2>
        <p className="hint workflow-timeline__hint">Each step is read separately and may update at different times.
          A missing provider result never proves something did not happen.</p>
        {steps.some((s) => s.isPending) && <Loading label="Loading step evidence…" />}
        {steps.some((s) => s.isError) && <p className="notice warn">The timeline is incomplete because some step evidence is unavailable.</p>}
        <ol className="timeline timeline--compact">
          {timeline.map((event) => <li key={event.id}>
            <span className={`timeline__node ${event.tone}`}><Icon name={event.icon} /></span>
            <p><Link to={`/actions/${event.operationId}`}>{event.step}</Link>: {event.text}
              {" "}<span className="muted small"><RelativeTime value={event.at} /></span></p>
          </li>)}
        </ol>
      </section>
      <TechnicalDetails>
        <dl className="facts">
          <dt>Workflow id</dt><dd><code>{w.workflow_id}</code></dd>
          <dt>Workflow type</dt><dd><code>{w.workflow_type}</code>, status <code>{w.status}</code></dd>
          <dt>Request key</dt><dd><code>{w.request_key}</code></dd>
          <dt>Created by</dt><dd><code>{w.created_by}</code></dd>
          <dt>Support case</dt><dd><code>{w.plan.order.resource_id}</code></dd>
          <dt>Compensation policy</dt><dd><code>{w.plan.compensation?.policy ?? "—"}</code></dd>
          {w.steps.map((step) => (
            <Fragment key={step.operation_id}>
              <dt>Step <code>{step.step}</code></dt>
              <dd><code>{step.operation_id}</code> · <code>{step.state}</code></dd>
            </Fragment>
          ))}
          {references.map((r, i) => (
            <Fragment key={i}>
              <dt>{r.step} reference</dt><dd><code>{r.reference}</code></dd>
            </Fragment>
          ))}
        </dl>
      </TechnicalDetails>
      <button type="button" className="secondary" onClick={() => {
        void workflow.refetch();
        steps.forEach((step) => { void step.refetch(); });
      }}>Refresh</button>
      </div>
    </div>
  );
}
