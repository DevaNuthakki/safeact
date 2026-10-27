// Published qualification v1 (2026-10-25), not metrics from this running demo.
// The evidence consistency test compares these fields with the sealed reports.
export const RESULTS = {
  date: "2026-10-25",
  scenarios: 1200,
  scoredRuns: 6000,
  prohibitedDuplicates: 0,
  pairedScenarios: 1137,
  safeact: { failures: 0, completed: 861, achievable: 874 },
  baseline: { failures: 0, completed: 841, achievable: 874 },
  full: { completed: 924, achievable: 937 },
} as const;

// The README's "What this doesn't prove" headings, in its order. The evidence
// consistency test fails if the README's list and this one differ.
export const LIMITS = [
  "One laptop",
  "Mock providers plus one Stripe sandbox",
  "No production operation",
  "The author wrote the test material",
  "The agent evaluation is small",
  "Human review is not measured",
  "One operation per target for agents",
  "Worker identity binding is open",
  "Stripe success is acceptance, not settlement",
  "No universal exactly-once",
] as const;

/**
 * The evidence documents bundled under ui/public/evidence/, each an exact copy
 * of a repository file, or of one section of it under a short header. The
 * consistency test fails on any edited, stale or undeclared copy.
 */
export const EVIDENCE: Record<string, { title: string; source: string; file: string;
  section?: { from: string; to: string; header: string } }> = {
  results: { title: "Full qualification results", source: "docs/phase-e/qual-v1/full/REVIEW.md",
    file: "docs/phase-e/qual-v1/full/REVIEW.md" },
  agents: { title: "Agent evaluation and scoring rules", source: "README.md — Agents (Milestone 4, held-out)",
    file: "README.md", section: { from: "### Agents (Milestone 4, held-out)", to: "### Stripe test mode",
      header: "# Agent evaluation and scoring rules\n\nSource: README.md, Agents (Milestone 4, held-out).\n\n" } },
  architecture: { title: "Architecture and evidence", source: "README.md", file: "README.md" },
  workflow: { title: "Conditional refund workflow", source: "docs/demo/customer-support.md",
    file: "docs/demo/customer-support.md" },
};

/** The bundled copy a declared source should produce: the file, or the header plus its section. */
export function evidenceCopy(original: string, section?: { from: string; to: string; header: string }) {
  if (!section) return original;
  const start = original.indexOf("\n", original.indexOf(section.from)) + 1;
  return section.header + original.slice(start, original.indexOf(section.to, start));
}
