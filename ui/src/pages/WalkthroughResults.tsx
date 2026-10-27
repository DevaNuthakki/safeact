import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router";

import { Icon } from "../components/Icon";
import { usePageTitle } from "../components/pageTitle";
import { ErrorState, Loading } from "../components/States";
import { ChapterFooter, ChapterHeading } from "../components/WalkthroughLayout";
import { EVIDENCE, LIMITS, RESULTS } from "../demo/results";

export function ResultsChapter() {
  usePageTitle("Results & limits");
  return <div className="story-stack">
    <ChapterHeading title="Measured results. Clear limits." description="Recorded evaluation evidence for this portfolio project. These are published results, not live dashboard metrics." />
    <dl className="story-results-strip"><div><Icon name="file" /><dt>Fault scenarios</dt><dd>{RESULTS.scenarios.toLocaleString("en-US")}</dd></div>
      <div><Icon name="search" /><dt>Scored scenario–system runs</dt><dd>{RESULTS.scoredRuns.toLocaleString("en-US")}</dd></div>
      <div><Icon name="shield" /><dt>SafeAct prohibited duplicates</dt><dd>{RESULTS.prohibitedDuplicates}</dd></div></dl>
    <div className="story-grid story-grid--wide">
      <section className="panel" aria-labelledby="paired-results"><h2 id="paired-results">Same scenarios, head to head</h2>
        <p className="muted small">{RESULTS.pairedScenarios.toLocaleString("en-US")} scenarios shared by SafeAct and the robust conventional baseline.</p>
        <p className="muted small">Useful / achievable: of the {RESULTS.safeact.achievable} operations both systems could complete in these scenarios, how many each finished.</p>
        <div className="table-wrap story-results-table"><table><caption className="visually-hidden">Recorded paired qualification results</caption>
          <thead><tr><th scope="col">System</th><th scope="col">Safety failures</th><th scope="col">Useful / achievable</th></tr></thead>
          <tbody><tr><th scope="row">SafeAct</th><td>{RESULTS.safeact.failures}</td><td>{RESULTS.safeact.completed} / {RESULTS.safeact.achievable}</td></tr>
            <tr><th scope="row">B3 · Robust conventional baseline</th><td>{RESULTS.baseline.failures}</td><td>{RESULTS.baseline.completed} / {RESULTS.baseline.achievable}</td></tr></tbody>
        </table></div>
        <p className="story-callout info"><Icon name="info" /><span>Both systems had zero safety failures. SafeAct completed {RESULTS.safeact.completed - RESULTS.baseline.completed} more achievable operations in this paired comparison.</span></p>
      </section>
      <div className="story-stack story-stack--compact">
        <section className="panel" aria-labelledby="demonstrated"><h2 id="demonstrated">What this demonstrates</h2><ul className="story-checks">
          {["Durable intent before dispatch", "Verification after uncertain responses", "Agent authority boundaries", "Evidence-bound human approval"].map((text) => <li key={text}><Icon name="check" />{text}</li>)}
        </ul></section>
        <section className="panel" aria-labelledby="limits"><h2 id="limits">What this does not prove</h2><ul className="story-checks">
          {LIMITS.map((text) => <li key={text}><Icon name="info" />{text}</li>)}
        </ul><p className="small"><Link to="/demo/evidence/architecture">Each limit explained (README) →</Link></p></section>
      </div>
    </div>
    <p className="story-callout warn"><Icon name="alert" /><span><strong>Full qualification:</strong> {RESULTS.full.achievable - RESULTS.full.completed} achievable completions were missed during rate-limit storms and short outages. Tested on one laptop with mock providers. Separate Stripe evidence is test-mode only.</span></p>
    <div className="story-source-links"><Link to="/demo/evidence/results"><Icon name="file" />Full results</Link><Link to="/demo/evidence/agents"><Icon name="robot" />Agent evaluation & scoring rules</Link><Link to="/demo/evidence/architecture"><Icon name="shield" />Architecture & evidence</Link></div>
    <section className="panel story-extra" aria-labelledby="additional-workflow"><Icon name="box" /><div><h2 id="additional-workflow">Additional capability: conditional refunds</h2>
      <p>Shipping charge → replacement order → refund only after confirmed order failure.</p><p className="muted small">An uncertain order is investigated; it is not automatically refunded. This implemented feature is outside the main walkthrough.</p>
    </div><Link to="/demo/evidence/workflow">Read workflow documentation →</Link></section>
    <p className="muted small">Qualification v1 · Recorded {RESULTS.date} · Outcomes are observations within the declared contracts and tested faults.</p>
    <ChapterFooter index={4} />
  </div>;
}


export function WalkthroughEvidence() {
  const { document = "" } = useParams();
  const info = EVIDENCE[document];
  usePageTitle(info?.title ?? "Evidence unavailable");
  const query = useQuery({
    queryKey: ["walkthrough-document", document], enabled: Boolean(info),
    queryFn: async ({ signal }) => {
      const response = await fetch(`/evidence/${document}.md`, { signal });
      const text = await response.text();
      if (!response.ok || /^\s*<!doctype html/i.test(text)) throw new Error("Evidence document unavailable");
      return text;
    },
  });
  return <div className="story-stack"><Link className="back-link" to="/demo/results"><Icon name="arrowLeft" />Results & limits</Link>
    <ChapterHeading title={info?.title ?? "Evidence document not found"} description={info ? `Bundled copy from ${info.source}. Technical reproduction commands are included for reference.` : undefined} />
    {query.isPending && info && <Loading label="Loading evidence document…" />}
    {query.isError && <ErrorState error={query.error} retry={() => void query.refetch()} />}
    {query.data && <><a href={`/evidence/${document}.md`} download>Download the source document</a><pre className="panel story-document">{query.data}</pre></>}
  </div>;
}
