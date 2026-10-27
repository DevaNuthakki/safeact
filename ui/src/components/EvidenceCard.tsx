import type { Attempt, EvidenceRow } from "../api/types";
import { humanize, refundStatusLabel, reportedRefundStatus } from "./labels";
import { Badge } from "./States";
import { RelativeTime } from "./time";

const OUTCOME: Record<string, { label: string; tone: string }> = {
  applied: { label: "Applied", tone: "ok" },
  confirmed_applied: { label: "Applied", tone: "ok" },
  not_applied: { label: "Not applied", tone: "bad" },
  proven_not_applied: { label: "Not applied", tone: "bad" },
  may_have_dispatched: { label: "May have been sent", tone: "warn" },
};

function outcome(code: string) {
  return OUTCOME[code] ?? { label: humanize(code), tone: "neutral" };
}

/** Provider- or caller-supplied data: shown as text, never as markup. */
export function Raw({ value }: { value: unknown }) {
  return <code className="raw">{JSON.stringify(value, null, 2)}</code>;
}

export function Provenance({ row }: { row: Pick<EvidenceRow, "provenance"> }) {
  return row.provenance === "human_attested"
    ? <Badge tone="warn" icon="user">Reviewer attested</Badge>
    : <Badge tone="neutral" icon="dot">System observation</Badge>;
}

const sameKind = (a: EvidenceRow, b: EvidenceRow) => a.outcome === b.outcome
  && a.evidence_kind === b.evidence_kind && a.evidence_reference === b.evidence_reference
  && a.provenance === b.provenance && a.observed_by === b.observed_by;

/** Consecutive identical observations become one card; every record stays listed. */
export function groupEvidence(rows: EvidenceRow[]) {
  const groups: EvidenceRow[][] = [];
  for (const row of rows) {
    const last = groups.at(-1);
    if (last && sameKind(last[0]!, row)) last.push(row);
    else groups.push([row]);
  }
  return groups;
}

export function AttemptCard({ attempt, max }: { attempt: Attempt; max: number | null }) {
  return (
    <section className="panel attempt" aria-labelledby={`attempt-${attempt.attempt_id}`}>
      <div className="panel__header">
        <h3 id={`attempt-${attempt.attempt_id}`}>Attempt {attempt.ordinal}{max ? ` of ${max}` : ""}</h3>
        <span className="muted small">
          {attempt.outcomes.length} {attempt.outcomes.length === 1 ? "record" : "records"} ·
          started <RelativeTime value={attempt.created_at} />
        </span>
      </div>
      {attempt.outcomes.length === 0 ? <p className="muted">No evidence recorded for this attempt yet.</p> : (
        <ul className="evidence">
          {groupEvidence(attempt.outcomes).map((group) => {
            const row = group[0]!;
            const result = outcome(row.outcome);
            const latest = group.at(-1)!;
            const refund = reportedRefundStatus(latest.details);
            return (
              <li key={`${row.observed_seq}-${row.created_at}`}>
                <div className="evidence__head">
                  <Badge tone={result.tone} icon={result.tone === "ok" ? "check" : result.tone === "bad" ? "x" : result.tone === "warn" ? "search" : "dot"}>
                    {result.label}
                  </Badge>
                  {row.evidence_kind && humanize(row.evidence_kind) !== result.label && <span>{humanize(row.evidence_kind)}</span>}
                  {group.length > 1 && <span className="muted">× {group.length}</span>}
                  <Provenance row={row} />
                </div>
                <p className="evidence__meta">
                  <span>{group.length > 1 ? "latest " : ""}<RelativeTime value={latest.created_at} /></span>
                  {refund !== null && (
                    <span> · Refund status reported at response time: <strong>{refundStatusLabel(refund)}</strong></span>
                  )}
                </p>
                <details className="disclosure">
                  <summary>Technical details (provider text, untrusted){group.length > 1 ? `, ${group.length} records` : ""}</summary>
                  <dl className="facts">
                    <dt>Outcome code</dt><dd><code>{row.outcome}</code>{row.evidence_kind && <> · <code>{row.evidence_kind}</code></>}</dd>
                    {row.evidence_reference && <><dt>Provider reference</dt><dd><code>{row.evidence_reference}</code></dd></>}
                    <dt>Observed by</dt><dd><code>{row.observed_by}</code> at version {row.observed_seq}</dd>
                  </dl>
                  <ul className="evidence__times">
                    {group.map((r, i) => (
                      <li key={i}>
                        <span className="small muted"><time dateTime={r.created_at}>{new Date(r.created_at).toLocaleString()}</time></span>
                        <Raw value={r.details} />
                      </li>
                    ))}
                  </ul>
                </details>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
