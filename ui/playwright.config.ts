import { defineConfig, devices } from "@playwright/test";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

// The browser tests drive the real console against the real stack: a
// throwaway database environment, uvicorn, relay, Celery over Redis,
// reconciler and advancer (deploy/demo/e2e_server.py), behind the Vite
// dev server's same-origin proxy. Assertions check backend state
// through the API, not the DOM alone.
export const ACCOUNTS_FILE = process.env.SAFEACT_E2E_ACCOUNTS
  ?? join(tmpdir(), `safeact-e2e-accounts-${process.pid}.json`);
process.env.SAFEACT_E2E_ACCOUNTS = ACCOUNTS_FILE;

// Every run keeps its own artifacts: Playwright empties its output
// folder when a run starts, so each run gets a folder of its own under
// test-results/ (CI uploads the whole folder on failure). Computed once
// and passed through the environment so worker processes agree.
process.env.SAFEACT_E2E_RUN ??= process.env.CI
  ? `ci-${process.env.GITHUB_RUN_ATTEMPT ?? "1"}`
  : `local-${new Date().toISOString().replace(/[:.]/g, "-")}`;
const RUN = process.env.SAFEACT_E2E_RUN;
const OUTPUT_DIR = join("test-results", RUN);
// Written by the e2e server before it starts: other SafeAct processes,
// ports and Redis clients at that moment, to diagnose an intermittent
// failure. Outside OUTPUT_DIR so the run's own cleanup never removes it.
process.env.SAFEACT_E2E_CONTEXT ??= resolve("test-results", `${RUN}-context.json`);

const API_PORT = 8001;
// Run from the repository root (cwd below).
const PYTHON = process.env.SAFEACT_PYTHON ?? ".venv/bin/python";

export default defineConfig({
  testDir: "e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 90_000,
  expect: { timeout: 30_000 },
  retries: 0,
  reporter: [["list"]],
  outputDir: OUTPUT_DIR,
  use: {
    baseURL: "http://127.0.0.1:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: [
    {
      command: `${PYTHON} -m deploy.demo.e2e_server --port ${API_PORT} --accounts ${ACCOUNTS_FILE} --context ${process.env.SAFEACT_E2E_CONTEXT}`,
      cwd: "..",
      url: `http://127.0.0.1:${API_PORT}/readyz`,
      timeout: 120_000,
      reuseExistingServer: false,
      stdout: "pipe",
      gracefulShutdown: { signal: "SIGTERM", timeout: 20_000 },
    },
    {
      command: "npx vite --port 5173 --strictPort",
      url: "http://127.0.0.1:5173/",
      env: { SAFEACT_API_URL: `http://127.0.0.1:${API_PORT}` },
      timeout: 60_000,
      reuseExistingServer: false,
    },
  ],
});
