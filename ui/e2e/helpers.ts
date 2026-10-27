import { readFileSync } from "node:fs";

import AxeBuilder from "@axe-core/playwright";

import { expect } from "@playwright/test";
import type { Browser, BrowserContext, Page } from "@playwright/test";

import { CASES, EMAIL, ORDER, REFUND, proposal } from "../src/test/storyFixtures";

type AxePage = ConstructorParameters<typeof AxeBuilder>[0]["page"];

export async function noSeriousViolations(page: Page, name: string) {
  // @axe-core/playwright types its page against playwright-core; same object.
  const results = await new AxeBuilder({ page: page as unknown as AxePage })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  const serious = results.violations.filter((v) => ["serious", "critical"].includes(v.impact ?? ""));
  expect(serious.map((v) => `${name}: ${v.id} (${v.nodes.length})`)).toEqual([]);
}

interface Accounts {
  schema: string;
  accounts: Record<string, string>;
}

let cached: Accounts | undefined;

export function accounts(): Accounts {
  cached ??= JSON.parse(readFileSync(process.env.SAFEACT_E2E_ACCOUNTS!, "utf8")) as Accounts;
  return cached;
}

/** Sign in through the real form, keyboard only. */
export async function signIn(page: Page, username: string) {
  await page.goto("/login?console=1");
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(accounts().accounts[username]!);
  await page.getByLabel("Password").press("Enter");
  await expect(page.getByRole("heading", { name: "Actions" })).toBeVisible();
}

export async function personContext(browser: Browser, username: string) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await signIn(page, username);
  return { context, page };
}

interface Answer { status: number; body: string }

/** One request from inside the page, with the browser's own session
 *  (Secure cookies on 127.0.0.1 are sent by the browser, not by
 *  Playwright's separate request client). */
export async function inPage(page: Page, path: string, init: { method?: string; body?: unknown } = {}) {
  return page.evaluate(async ({ path, init }): Promise<Answer> => {
    const csrf = document.cookie.split("; ").find((c) => c.startsWith("__Host-safeact_csrf="))?.split("=")[1];
    const response = await fetch(path, {
      method: init.method ?? "GET",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        ...(init.method && init.method !== "GET" && csrf ? { "X-SafeAct-CSRF": csrf } : {}),
      },
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
    });
    return { status: response.status, body: await response.text() };
  }, { path, init });
}

/** The backend's answer, with the page's own session (not the DOM). */
export async function backend<T>(page: Page, path: string): Promise<T> {
  const answer = await inPage(page, path);
  expect(answer.status, `${path}: ${answer.body}`).toBe(200);
  return JSON.parse(answer.body) as T;
}

/** Submit over the API as a requester (setup for journeys 2 and 3). */
export async function submitAs(page: Page, body: Record<string, unknown>) {
  const answer = await inPage(page, "/actions", {
    method: "POST", body: { request_key: `e2e-${crypto.randomUUID()}`, ...body },
  });
  expect(answer.status, answer.body).toBe(201);
  return (JSON.parse(answer.body) as { operation_id: string }).operation_id;
}

export async function waitForState(page: Page, operationId: string, states: string[], timeout = 45_000) {
  await expect.poll(async () => (await backend<{ state: string }>(page, `/actions/${operationId}`)).state,
                    { timeout }).toMatch(new RegExp(`^(${states.join("|")})$`));
}

/**
 * A screen that cannot refresh: no live channel, and every later read of
 * this proposal answers with the view it first loaded. The console would
 * otherwise re-read on a live update, focus or reconnect and hide
 * Approve (which is right); this lets a test click on an old screen and
 * prove the API refuses it. Decisions still go to the real API.
 * Call before the page first loads the proposal.
 */
export async function freezeProposalScreen(context: BrowserContext, proposalId: string) {
  await context.route("**/events**", (route) => route.abort("connectionreset"));
  let firstView: string | undefined;
  await context.route(`**/proposals/${proposalId}`, async (route) => {
    // Only the console's API reads; the page navigation itself loads the app.
    if (route.request().method() !== "GET" || route.request().resourceType() !== "fetch") {
      return route.fallback();
    }
    if (firstView === undefined) firstView = await (await route.fetch()).text();
    return route.fulfill({ status: 200, contentType: "application/json", body: firstView });
  });
}

/** Wait for the submit form itself, not for the page being left. */
export async function onSubmitForm(page: Page) {
  await expect(page.getByRole("heading", { name: "Submit an action" })).toBeVisible();
}

/**
 * The submit form keeps a request in sessionStorage
 * ("safeact.submit.pending.<owner>", src/api/requestKey.ts) until the
 * action page shows its recorded operation (src/pages/ActionDetail.tsx,
 * the release effect). Opening /submit before that shows "Request retained
 * in this browser" instead of a new form, by design. Wait for the release.
 */
export async function retainedRequestReleased(page: Page) {
  await expect.poll(() => page.evaluate(() =>
    Object.keys(sessionStorage).filter((key) => key.startsWith("safeact.submit.pending.")).length,
  ), { message: "the retained request is released once its action is on screen" }).toBe(0);
}

/** Serve the walkthrough's case index and its reads from fixtures; everything else is real. */
export async function routeStoryFixtures(context: BrowserContext) {
  const reads = new Map<string, unknown>([
    ["/demo-cases.json", CASES],
    [`/actions/${CASES.refund.operation_id}`, REFUND],
    [`/actions/${CASES.email.operation_id}`, EMAIL],
    [`/actions/${CASES.order.operation_id}`, ORDER],
    [`/proposals/${CASES.email.proposal_id}`, proposal()],
    [`/proposals/${CASES.order.proposal_id}`, proposal(false)],
  ]);
  await context.route("**/*", (route) => {
    const request = route.request();
    const body = reads.get(new URL(request.url()).pathname);
    return body && request.method() === "GET" && request.resourceType() === "fetch"
      ? route.fulfill({ contentType: "application/json", body: JSON.stringify(body) })
      : route.fallback();
  });
}

/**
 * Visible elements that leave the viewport, or that a scroll container clips
 * (content hidden behind a horizontal scroll counts as overflow). Visually
 * hidden content, the skip link and closed <details> bodies are not shown.
 */
export async function overflowing(page: Page) {
  return page.evaluate(() => {
    const found: string[] = [];
    for (const el of document.querySelectorAll("body *")) {
      const r = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      if (!r.width || !r.height || style.visibility === "hidden" || style.display === "none") continue;
      if (el.closest(".visually-hidden, .skip-link")) continue;
      if (el.closest("details:not([open])") && !el.closest("summary")) continue;
      let hidden = false;
      let clip: DOMRect | null = null;
      for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
        const ar = a.getBoundingClientRect();
        const as = getComputedStyle(a);
        if (ar.width <= 1 && ar.height <= 1 && as.overflow === "hidden") { hidden = true; break; }
        if (!clip && as.overflowX !== "visible") clip = ar;
      }
      if (hidden) continue;
      const clipped = clip !== null && (r.right > clip.right + 0.5 || r.left < clip.left - 0.5);
      if (r.left < -0.5 || r.right > innerWidth + 0.5 || clipped) {
        found.push(`${el.tagName.toLowerCase()}${el.className ? `.${String(el.className).split(" ").join(".")}` : ""} "${(el.textContent ?? "").trim().slice(0, 40)}"`);
      }
    }
    return { scrollWidth: document.documentElement.scrollWidth, innerWidth, found };
  });
}
