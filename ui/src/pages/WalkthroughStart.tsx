import { Link } from "react-router";

import { Icon } from "../components/Icon";
import type { IconName } from "../components/Icon";
import { money } from "../components/labels";
import { usePageTitle } from "../components/pageTitle";
import { ChapterHeading } from "../components/WalkthroughLayout";
import { STORY_REFUND_CENTS } from "../demo/walkthrough";

const CASES: { path: string; icon: IconName; title: string; text: string }[] = [
  { path: "/demo/refund", icon: "card", title: `${money(STORY_REFUND_CENTS)} refund`, text: "Applied once. Verified after a lost response." },
  { path: "/demo/agent", icon: "mail", title: "Agent-created email", text: "The agent cannot attest that it succeeded." },
  { path: "/demo/decision", icon: "user", title: "Human decision", text: "An investigator proposes. A separate reviewer decides." },
];

export function WalkthroughStart() {
  usePageTitle("Start here");
  return <div className="story-stack">
    <ChapterHeading title="When a response is lost, did the action happen?" description="A timeout can hide a completed refund or a sent email. Retrying blindly can repeat the effect." />
    <div className="story-grid">
      <section className="panel" aria-labelledby="risk-title"><h2 id="risk-title">The risk</h2>
        <ol className="risk-flow"><li>Send request</li><li>Lose response</li><li>Outcome uncertain</li></ol>
        <p className="story-callout warn"><Icon name="alert" /><strong>A timeout is not proof that nothing happened.</strong></p>
      </section>
      <section className="panel" aria-labelledby="approach-title"><h2 id="approach-title">What SafeAct does</h2>
        <ol className="story-principles">
          <li><strong>Records the request first</strong><p>Saves a durable record before taking action.</p></li>
          <li><strong>Checks evidence before another attempt</strong><p>Uses provider lookup where available. A retry needs a safe basis.</p></li>
          <li><strong>Requires an independent human decision when needed</strong><p>An investigator proposes. A different reviewer decides.</p></li>
        </ol>
      </section>
    </div>
    <div className="story-cases">{CASES.map((item) => <Link className="panel story-case" key={item.path} to={item.path}>
      <span className="icon-circle info"><Icon name={item.icon} /></span><span><strong>{item.title}</strong><span>{item.text}</span></span><span aria-hidden="true">→</span>
    </Link>)}</div>
    <div className="story-start-footer"><Link className="button" to="/demo/refund">Start with the refund <span aria-hidden="true">→</span></Link>
      <p className="muted">Local mock demo · Synthetic data · Scripted MCP calls</p></div>
    <p className="story-signin-note">One reviewer sign-in for this walkthrough. The agent request and the investigator's proposal are prepared as separate identities.</p>
  </div>;
}
