import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import { PREP_STEPS } from "../components/WalkthroughLayout";
import { EVIDENCE, LIMITS, RESULTS, evidenceCopy } from "./results";

const read = (path: string) => readFileSync(resolve(process.cwd(), "..", path), "utf8");

it("keeps displayed results consistent with the recorded qualification", () => {
  const full = JSON.parse(read("docs/phase-e/qual-v1/full/summary.json"));
  const paired = JSON.parse(read("docs/phase-e/qual-v1/paired/paired.json")).paired;
  expect(RESULTS.scenarios).toBe(full.by_arm.safeact.scenario_arms);
  expect(RESULTS.scoredRuns).toBe(Object.values(full.by_arm).reduce((n: number, arm) => n + (arm as { scenario_arms: number }).scenario_arms, 0));
  expect(RESULTS.prohibitedDuplicates).toBe(full.by_arm.safeact.prohibited_duplicates);
  expect(full.by_arm.safeact.usefulness).toMatchObject(RESULTS.full);
  expect(RESULTS.pairedScenarios).toBe(paired.safeact.scenarios);
  for (const [display, source] of [[RESULTS.safeact, paired.safeact], [RESULTS.baseline, paired.b3]]) {
    expect(display).toEqual({ failures: source.safety_failure_rows, completed: source.usefulness.completed, achievable: source.usefulness.achievable });
  }
});

it("bundles every evidence document as an exact copy of its declared source", () => {
  const bundled = readdirSync(resolve(process.cwd(), "public/evidence")).sort();
  expect(bundled).toEqual(Object.keys(EVIDENCE).map((name) => `${name}.md`).sort());
  for (const [name, { file, section }] of Object.entries(EVIDENCE)) {
    expect(read(`ui/public/evidence/${name}.md`), name).toBe(evidenceCopy(read(file), section));
  }
});

it("lists exactly the README's limits, in its order", () => {
  const readme = read("README.md");
  const start = readme.indexOf("## What this doesn't prove");
  const section = readme.slice(start, readme.indexOf("\n## ", start + 1));
  const headings = [...section.matchAll(/^- \*\*(.+?)\*\*/gm)].map((m) => m[1]!.replace(/[.:]$/, ""));
  expect(headings.length).toBeGreaterThan(0);
  expect([...LIMITS]).toEqual(headings);
});

it("shows the README's fresh-clone walkthrough commands on the unprepared page", () => {
  // Shell line continuations in the README are joined before comparing.
  const readme = read("README.md").replace(/\\\n\s*/g, "");
  for (const step of PREP_STEPS) {
    const command = step.replace("cd ui && npm ci && ", "");
    expect(readme).toContain(command);
  }
  expect(readme).toContain("cd ui && npm ci && SAFEACT_API_URL=http://127.0.0.1:8080 npm run dev");
});
