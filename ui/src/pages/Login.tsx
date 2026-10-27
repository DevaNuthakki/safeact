import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router";

import { ApiError } from "../api/client";
import type { IconName } from "../components/Icon";
import { Icon } from "../components/Icon";
import { Brand } from "../components/Layout";
import { usePageTitle } from "../components/pageTitle";
import { ErrorState } from "../components/States";
import { useSession } from "../session/SessionProvider";

const BENEFITS: { icon: IconName; title: string; text: string }[] = [
  { icon: "file", title: "Recorded first.", text: "Every action is saved before anything is sent." },
  { icon: "search", title: "Evidence before another attempt.", text: "Uncertain outcomes are checked where the provider supports it. Retries must satisfy its safety contract." },
  { icon: "user", title: "People stay accountable.", text: "MCP agents can request and investigate. A separate human reviewer decides restricted recovery." },
];

export function Login() {
  usePageTitle("Sign in");
  const { signIn } = useSession();
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    setPending(true);
    setError(null);
    try {
      await signIn(username.trim(), password);
      const from = (location.state as { from?: string } | null)?.from;
      const consoleSignIn = new URLSearchParams(location.search).get("console") === "1";
      navigate(from?.startsWith("/") && !from.startsWith("//") && from !== "/login" ? from : consoleSignIn ? "/actions" : "/demo", { replace: true });
    } catch (caught) {
      setError(caught);
      setPassword("");
    } finally {
      setPending(false);
    }
  };

  const failed = error instanceof ApiError && error.code === "invalid_credentials";

  return (
    <main id="main" className="auth">
      <div className="auth__split">
        <section className="auth__story" aria-labelledby="auth-story-title">
          <p className="auth__brand"><Brand /></p>
          <p id="auth-story-title" className="auth__headline">One story.<br />An independent decision.</p>
          <p className="auth__intro">Follow a lost-response refund, a blocked agent attestation, and the permitted human decision. The guided walkthrough uses one reviewer sign-in.</p>
          <ul className="auth__benefits">
            {BENEFITS.map((b) => (
              <li key={b.title}>
                <span className="icon-circle info"><Icon name={b.icon} /></span>
                <span><strong>{b.title}</strong><span className="muted"> {b.text}</span></span>
              </li>
            ))}
          </ul>
        </section>
        <div className="auth__card stack">
          <div className="panel">
            <h1>Sign in</h1>
            <p className="muted">For the guided demo, use the reviewer account you were given. Requests and investigator proposals are prepared separately.</p>
            <form className="stack" onSubmit={submit} noValidate aria-describedby={failed ? "login-error" : undefined}>
              <div className="field">
                <label htmlFor="username">Username</label>
                <input id="username" name="username" autoComplete="username" required
                       value={username} onChange={(e) => setUsername(e.target.value)} />
              </div>
              <div className="field">
                <label htmlFor="password">Password</label>
                <div className="password-field">
                <input id="password" name="password" type={showPassword ? "text" : "password"} autoComplete="current-password"
                       required value={password} onChange={(e) => setPassword(e.target.value)} />
                <button type="button" className="ghost" aria-pressed={showPassword} onClick={() => setShowPassword(!showPassword)}>{showPassword ? "Hide password" : "Show password"}</button>
                </div>
              </div>
              {failed && (
                <p id="login-error" className="notice bad" role="alert">
                  Sign-in failed. Check your username and password, or try again later.
                </p>
              )}
              {error !== null && !failed && <ErrorState error={error} />}
              <button type="submit" className="block" disabled={pending || !username || !password}>
                {pending ? "Signing in…" : "Sign in"}
              </button>
            </form>
          </div>
          <p className="auth__footnote">Local demo · Synthetic accounts. Mock providers or an explicitly enabled Stripe test-mode profile.</p>
          <p className="auth__footnote">Connecting an AI agent? <Link to="/mcp-guide">Open the MCP guide</Link></p>
          <p className="auth__footnote"><Link to="/demo">Read the introduction without signing in</Link></p>
        </div>
      </div>
    </main>
  );
}
