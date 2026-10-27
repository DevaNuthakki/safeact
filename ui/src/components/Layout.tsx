import { useEffect, useRef } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router";

import { useLiveStatus } from "../live/LiveUpdates";
import { useSession } from "../session/SessionProvider";
import { Icon } from "./Icon";
import { useTheme } from "./theme";
import { UserMenu } from "./UserMenu";

const LIVE_TEXT = {
  connecting: "Connecting to live updates…",
  live: "Auto-updating",
  reconnecting: "Reconnecting… changes will catch up",
  offline: "Live updates unavailable. Refresh to retry.",
} as const;

export function Brand() {
  return <><Icon name="shield" className="brand__mark" /><span>SafeAct</span></>;
}

export function ThemeSwitch() {
  const [theme, choose] = useTheme();
  const dark = theme === "dark";
  return (
    <button type="button" className="icon-button" aria-pressed={dark} onClick={() => choose(dark ? "light" : "dark")}
            title={dark ? "Switch to the light theme" : "Switch to the dark theme"}>
      <Icon name={dark ? "sun" : "moon"} /><span className="visually-hidden">Dark theme</span>
    </button>
  );
}

export function Layout() {
  const { principal, has, signOut } = useSession();
  const live = useLiveStatus();
  const navigate = useNavigate();
  const location = useLocation();
  const main = useRef<HTMLElement>(null);

  // Move focus to the page content after navigation, for keyboard and
  // screen-reader users.
  useEffect(() => {
    main.current?.focus();
  }, [location.pathname]);

  return (
    <>
      <a className="skip-link" href="#main">Skip to main content</a>
      <header className="app-header">
        <div className="app-header__inner">
          <Link to="/actions" className="brand">
            <Brand />
          </Link>
          {/* Navigation hides what a role cannot use; the API still decides. */}
          <nav className="app-nav" aria-label="Main">
            <ul>
              <li><NavLink to="/actions" end>Actions</NavLink></li>
              {(has("investigator") || has("reviewer")) && (
                <li><NavLink to="/proposals">Reviews</NavLink></li>
              )}
              {has("requester") && <li><NavLink to="/workflows/new">Send a replacement</NavLink></li>}
            </ul>
          </nav>
          <div className="app-header__end">
            <NavLink to="/mcp-guide" className="header-help">MCP guide</NavLink>
            <NavLink to="/demo" className="header-help">Guided demo</NavLink>
            {principal?.demo && <span className="demo-label">Demo</span>}
            <span className={`live-pill ${live === "live" ? "ok" : live === "offline" ? "bad" : "warn"}`}
                  title={live === "live" ? "This page updates by itself when actions change." : LIVE_TEXT[live]}>
              <span className="live-pill__dot" aria-hidden="true" />
              <span className="live-pill__text" role="status" aria-live="polite">{LIVE_TEXT[live]}</span>
              {live === "live" && <span className="visually-hidden">: this page updates when actions change</span>}
            </span>
            <ThemeSwitch />
            {principal && (
              <UserMenu principal={principal} onSignOut={async () => {
                await signOut();
                navigate("/login");
              }} />
            )}
          </div>
        </div>
      </header>
      <main id="main" ref={main} tabIndex={-1}>
        <Outlet />
      </main>
    </>
  );
}
