import { useEffect, useRef } from "react";
import type { ReactNode } from "react";
import { Link, Navigate, NavLink, Outlet, useLocation, useNavigate } from "react-router";

import { LiveUpdates } from "../live/LiveUpdates";
import { useSession } from "../session/SessionProvider";
import { Brand, ThemeSwitch } from "./Layout";
import { Icon } from "./Icon";
import { Loading } from "./States";

export const CHAPTERS = [
  { path: "/demo", label: "Start here" },
  { path: "/demo/refund", label: "The refund" },
  { path: "/demo/agent", label: "The agent case" },
  { path: "/demo/decision", label: "Human decision" },
  { path: "/demo/results", label: "Results & limits" },
];

export function WalkthroughLayout() {
  const { principal, has, signOut } = useSession();
  const location = useLocation();
  const navigate = useNavigate();
  const main = useRef<HTMLElement>(null);
  useEffect(() => { main.current?.focus(); }, [location.pathname]);
  return <div className="walkthrough">
    <a className="skip-link" href="#main">Skip to main content</a>
    <header className="story-header"><div className="story-header__inner">
      <div className="row story-header__brand"><Link to="/demo" className="brand"><Brand /></Link><span className="demo-label">Portfolio demo</span></div>
      <div className="row story-header__nav"><Link to="/actions">Inspect console</Link><Link to="/mcp-guide">Help</Link><ThemeSwitch /></div>
      <div className="story-header__account">
        {principal ? <details className="story-account"><summary><Icon name="user" />{has("reviewer") ? "Reviewer" : "Account"}</summary>
          <div className="story-account__body"><p>{principal.display_name}</p><p className="muted">{principal.username}</p>
            <button type="button" className="secondary" onClick={async () => { await signOut(); navigate("/demo"); }}>Sign out</button>
          </div></details> : <Link to="/login" state={{ from: "/demo/refund" }}>Reviewer sign in</Link>}
      </div>
    </div></header>
    <nav className="story-chapters" aria-label="Demo chapters"><ol>{CHAPTERS.map((chapter, index) => <li key={chapter.path}>
      <NavLink to={chapter.path} end><span className="story-chapters__number" aria-hidden="true">{index + 1}</span>{chapter.label}</NavLink>
    </li>)}</ol></nav>
    <main id="main" ref={main} tabIndex={-1} className="story-main"><Outlet /></main>
  </div>;
}

export function RequireReviewer() {
  const { principal, loading, has, signOut } = useSession();
  const location = useLocation();
  const navigate = useNavigate();
  if (loading) return <Loading label="Checking your reviewer session…" />;
  if (!principal) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  if (!has("reviewer")) return <section className="panel story-access"><h1>Continue with the reviewer account</h1>
    <p>This guided demo uses one human reviewer sign-in. Requests and investigator proposals are prepared separately; permissions remain unchanged.</p>
    <p>You are currently signed in as <strong>{principal.display_name}</strong>.</p>
    <button type="button" onClick={async () => { await signOut(); navigate("/login", { state: { from: location.pathname } }); }}>Sign out and use reviewer account</button>
    <Link to="/actions">Return to your console</Link>
  </section>;
  return <LiveUpdates enabled><Outlet /></LiveUpdates>;
}

export function ChapterHeading({ title, description, status }: { title: string; description?: string; status?: ReactNode }) {
  return <div className="story-heading"><div><h1>{title}</h1>{description && <p>{description}</p>}</div>{status}</div>;
}

export function ChapterFooter({ index }: { index: number }) {
  const previous = CHAPTERS[index - 1];
  const next = CHAPTERS[index + 1];
  return <nav className="story-footer" aria-label="Continue walkthrough">
    {previous ? <Link className="button secondary" to={previous.path}><Icon name="arrowLeft" />Back: {previous.label}</Link> : <span />}
    {next ? <Link className="button" to={next.path}>Next: {next.label} <span aria-hidden="true">→</span></Link>
      : <Link className="button" to="/demo">Restart walkthrough <span aria-hidden="true">↻</span></Link>}
  </nav>;
}

/** Fresh-clone preparation, the same commands as the README's "Guided walkthrough". */
export const PREP_STEPS = [
  "python3.13 -m venv .venv",
  ".venv/bin/python -m pip install -r requirements-db.lock -r requirements-api.lock -r requirements-mcp.lock",
  ".venv/bin/python ui/scripts/prepare_walkthrough.py --compose",
  "cd ui && npm ci && SAFEACT_API_URL=http://127.0.0.1:8080 npm run dev",
];

export function CaseUnavailable({ retry, children }: { retry: () => void; children?: ReactNode }) {
  return <section className="panel story-access" aria-labelledby="case-unavailable"><span className="icon-circle warn"><Icon name="info" /></span>
    <h2 id="case-unavailable">The demo cases are not available</h2>
    <p>{children ?? "Prepare the local demo cases, then retry. If the demo was rebuilt, its old links no longer identify the current records."}</p>
    <div className="row"><button type="button" onClick={retry}>Retry loading cases</button><Link to="/actions">Inspect available actions</Link></div>
    <details className="tech"><summary>Local demo preparation</summary><div className="tech__body">
      <p>From the project root, with the Compose demo stack running (README, “Run it locally”). Needs Python 3.13 and Node 24.</p>
      <ol className="prep-steps">
        {PREP_STEPS.map((step) => <li key={step}><code className="raw">{step}</code></li>)}
        <li>Open http://127.0.0.1:5173/demo and sign in as demo-north-reviewer.</li>
      </ol>
      <p>Preparation submits any missing mock cases and writes a public index of ids. It never resets the database or publishes credentials. The console built into the Compose stack (port 8080) never includes this index, so its chapters always show this page.</p>
    </div></details>
  </section>;
}
