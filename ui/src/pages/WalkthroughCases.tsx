import { Link } from "react-router";

import type { OperationDetail } from "../api/types";
import { Actor } from "../components/Actor";
import { AttemptCard } from "../components/EvidenceCard";
import { Icon } from "../components/Icon";
import { money } from "../components/labels";
import { usePageTitle } from "../components/pageTitle";
import { Badge, Loading, StateBadge, successWording } from "../components/States";
import { TechnicalDetails } from "../components/TechnicalDetails";
import { Timeline } from "../components/Timeline";
import { RelativeTime } from "../components/time";
import { CaseUnavailable, ChapterFooter, ChapterHeading } from "../components/WalkthroughLayout";
import { useSession } from "../session/SessionProvider";
import { STORY_REFUND_CENTS, matchesStoryAction, useStoryAction, useWalkthroughCases } from "../demo/walkthrough";

const REFUND_AMOUNT = money(STORY_REFUND_CENTS);

export function StoryHistory({ operation }: { operation: OperationDetail }) {
  const ambiguous = operation.transitions.find((t) => t.to_state === "UNKNOWN");
  const checking = operation.transitions.find((t) => t.to_state === "VERIFYING");
  const succeeded = operation.transitions.find((t) => t.to_state === "SUCCEEDED");
  const escalated = operation.transitions.find((t) => t.to_state === "ESCALATED_UNRESOLVED");
  const noLookup = operation.contract?.lookup_semantics === "unavailable"
    || operation.attempts.some((attempt) => attempt.outcomes.some((outcome) => outcome.details.note === "no_lookup"));
  const events = [
    { title: "Request recorded", text: "Saved durably before anything was sent.", at: operation.created_at, tone: "info" },
    ...(ambiguous ? [{ title: "Response lost; outcome uncertain", text: "A timeout did not establish whether the effect happened.", at: ambiguous.created_at, tone: "warn" }] : []),
    ...(checking ? [{ title: noLookup ? "No reliable provider lookup" : "SafeAct checked with the provider",
      text: noLookup ? "The connector has no reliable lookup. The outcome could not be independently verified."
        : "The outcome was investigated using the connector's lookup.", at: checking.created_at, tone: "info" }] : []),
    ...(escalated ? [{ title: "Escalated for human evidence", text: "The outcome remained unresolved. No unsupported retry was allowed.", at: escalated.created_at, tone: "warn" }] : []),
    ...(succeeded ? [{ title: operation.success_provenance === "human_attested" ? "Success attested by a reviewer" : "Effect confirmed by provider evidence", text: operation.success_provenance === "human_attested" ? "Human attestation is recorded separately from provider verification." : "The provider's evidence settled the uncertainty.", at: succeeded.created_at, tone: "ok" }] : []),
  ];
  return <ol className="story-history">{events.map((event) => <li key={event.title}>
    <span className={`story-history__marker ${event.tone}`}><Icon name={event.tone === "ok" ? "check" : event.tone === "warn" ? "alert" : "dot"} /></span>
    <div><strong>{event.title}</strong><p>{event.text}</p></div><RelativeTime value={event.at} />
  </li>)}</ol>;
}

export function RefundChapter() {
  usePageTitle(`The ${REFUND_AMOUNT} refund`);
  const { principal } = useSession();
  const cases = useWalkthroughCases();
  const action = useStoryAction(cases.data?.refund.operation_id);
  const retry = () => { void cases.refetch(); if (cases.data) void action.refetch(); };
  if (cases.isError || action.isError) return <CaseUnavailable retry={retry} />;
  if (!cases.data || action.isPending) return <Loading label="Loading the recorded refund and its evidence…" />;
  const op = action.data;
  if (!op || !matchesStoryAction(op, "refund")) return <CaseUnavailable retry={retry}>The prepared refund does not match this demo's {REFUND_AMOUNT} mock case. Inspect the actual record before drawing a conclusion.</CaseUnavailable>;
  const verified = op.state === "SUCCEEDED" && op.success_provenance === "provider_verified";
  const lost = op.transitions.some((t) => t.to_state === "UNKNOWN");
  const demonstrated = verified && lost && op.attempts.length === 1;
  return <div className="story-stack">
    <ChapterHeading title={demonstrated ? `One ${REFUND_AMOUNT} refund. A lost response.` : `The ${REFUND_AMOUNT} refund`} status={<StateBadge state={op.state} />} />
    <p className="story-attribution"><span className="muted">Requested by</span><Actor id={op.requested_by} principal={principal} /></p>
    <div className={`notice ${demonstrated ? "ok" : "info"}`}><p className="notice__title"><Icon name={demonstrated ? "check" : "info"} />{demonstrated ? "The provider confirmed the refund. SafeAct did not dispatch it again." : "This is the current recorded outcome. Inspect the evidence below."}</p></div>
    <div className="story-grid story-grid--wide">
      <section className="panel" aria-labelledby="refund-history"><h2 id="refund-history">What happened</h2><StoryHistory operation={op} /></section>
      <section className="panel" aria-labelledby="refund-proof"><h2 id="refund-proof">Evidence</h2>
        <dl className="story-facts"><div><dt>Refund amount</dt><dd>{REFUND_AMOUNT}</dd></div><div><dt>Recorded attempts</dt><dd>{op.attempts.length}</dd></div><div><dt>Outcome</dt><dd className="story-facts__label">{verified ? "Provider verified" : op.success_provenance === "human_attested" ? "Reviewer-attested" : "Not yet verified"}</dd></div></dl>
        <p className="story-panel-note">{demonstrated ? "Lookup found the provider's record for this operation. One recorded attempt was needed." : "The page only claims verified completion when the live record supports it."}</p>
        <Link to={`/actions/${op.operation_id}?tab=evidence`}>Inspect the full evidence →</Link>
      </section>
    </div>
    <p className="story-callout warn"><Icon name="alert" /><span>A blind retry could have refunded it twice: <strong>{money(2 * STORY_REFUND_CENTS)} total</strong>. That is the hypothetical risk, not money lost in this demo.</span></p>
    <TechnicalDetails><Timeline transitions={op.transitions} recordedAt={op.created_at} provenance={op.success_provenance} /><p><Link to={`/actions/${op.operation_id}?tab=technical`}>Inspect operation and connector details</Link></p></TechnicalDetails>
    <ChapterFooter index={1} />
  </div>;
}

export function AgentChapter() {
  usePageTitle("The agent case");
  const cases = useWalkthroughCases();
  const action = useStoryAction(cases.data?.email.operation_id);
  const retry = () => { void cases.refetch(); if (cases.data) void action.refetch(); };
  if (cases.isError || action.isError) return <CaseUnavailable retry={retry} />;
  if (!cases.data || action.isPending) return <Loading label="Loading the agent's email and captured refusal…" />;
  const op = action.data;
  if (!op || !matchesStoryAction(op, "email")) return <CaseUnavailable retry={retry}>The email record does not match the prepared MCP agent case.</CaseUnavailable>;
  const trace = cases.data.agent_refusal;
  const attested = op.state === "SUCCEEDED" && op.success_provenance === "human_attested";
  return <div className="story-stack">
    <ChapterHeading title="The agent cannot mark its own work as done." description="This same email continues into the human-decision chapter." status={<StateBadge state={op.state} label={attested ? successWording(op.success_provenance).title : undefined} />} />
    <p className="story-attribution"><Icon name="robot" />Requested through MCP · Support assistant</p>
    <section className={`notice ${attested ? "info" : "warn"}`} aria-labelledby="email-outcome"><h2 id="email-outcome">{attested ? "A reviewer has since attested this email" : "The lost response left delivery uncertain"}</h2>
      <p>The email's response was lost. This provider has no reliable lookup, so SafeAct did not blindly resend it.{attested && " The recorded resolution is human-attested, not provider-verified."}</p>
    </section>
    <div className="story-grid">
      <section className="panel" aria-labelledby="email-history"><h2 id="email-history">Email history</h2><StoryHistory operation={op} />
        <dl className="story-facts"><div><dt>Customer</dt><dd className="story-facts__label">#3002</dd></div><div><dt>Recorded attempts</dt><dd>{op.attempts.length}</dd></div><div><dt>Automatic resend</dt><dd className="story-facts__label">Not allowed</dd></div></dl>
      </section>
      <section className="panel" aria-labelledby="agent-trace"><div className="panel__header"><h2 id="agent-trace">Captured MCP response</h2><Badge tone="neutral" icon="file">Recorded demo trace</Badge></div>
        <p className="muted small">Scripted MCP client · Captured <RelativeTime value={trace.captured_at} /></p>
        <pre className="story-code"><code>{`${trace.tool}\naction: ${trace.action}`}</code></pre>
        <div className="notice bad"><p className="notice__title"><Icon name="lock" />Refused: agents cannot attest that an effect was applied.</p><p>{trace.message}</p><p className="muted small">Technical detail: <code>{trace.error}</code></p></div>
        <p className="story-panel-note">Agents cannot attest applied effects or approve proposals. Their proposal vocabulary permits re-verification and protected retry only where eligible.</p>
        <p className="muted small">This response was captured during demo preparation. It is not a live event in the REST action timeline.</p>
      </section>
    </div>
    <p className="story-callout info"><Icon name="user" /><span>Next: the investigator's proposal for <strong>this same email</strong> goes to a different human reviewer.</span></p>
    <TechnicalDetails>{op.attempts.map((attempt) => <AttemptCard key={attempt.attempt_id} attempt={attempt} max={op.contract?.max_attempts ?? null} />)}<Link to={`/actions/${op.operation_id}`}>Inspect the email in the console</Link></TechnicalDetails>
    <ChapterFooter index={2} />
  </div>;
}
