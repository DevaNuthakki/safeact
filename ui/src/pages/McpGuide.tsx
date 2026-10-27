import { Link } from "react-router";

import { Icon } from "../components/Icon";
import { Brand } from "../components/Layout";
import { usePageTitle } from "../components/pageTitle";
import { useSession } from "../session/SessionProvider";

const TOOLS = [
  ["Request an action", "submit_action", "Submit one granted action with a stable intent reference."],
  ["Read its status", "get_action_status", "Follow the recorded outcome, including uncertainty."],
  ["Inspect recorded evidence", "inspect_evidence", "Read attempts, observations, and the action history."],
  ["Ask for a permitted lookup", "request_verification", "Request a lookup within the connector's limits; this never resends the action."],
  ["Propose a next step", "propose_resolution", "Propose re-verification or a protected retry for a human reviewer."],
];

/** Static help for the existing local MCP server; no keys or telemetry are read. */
export function McpGuide() {
  usePageTitle("MCP guide");
  const { principal } = useSession();
  const content = <div className="page page--guide">
    <Link to={principal ? "/actions" : "/login"} className="back-link"><Icon name="arrowLeft" />{principal ? "All actions" : "Back to sign in"}</Link>
    <div className="page-header"><div>
      <p className="eyebrow">AI agents · Local demo</p>
      <h1>Connect an AI agent</h1>
      <p className="page-header__lede">Agents request actions. SafeAct checks the evidence. People decide restricted recovery.</p>
    </div></div>
    <div className="stack">
      <ol className="guide-steps">
        <li className="panel"><span className="guide-step">1</span><h2>Connect your MCP client</h2>
          <p>Use the local demo's Streamable HTTP endpoint in your MCP client.</p>
          <code className="endpoint">http://127.0.0.1:8002/mcp</code>
          <p className="hint">Default local address. Use your configured port if it differs.</p>
        </li>
        <li className="panel"><span className="guide-step">2</span><h2>Use your scoped agent key</h2>
          <p>Use the agent key issued for your local demo, in the Authorization header with the Bearer scheme.</p>
          <p className="hint">Keys are not displayed or managed in this console. Available tools depend on the key's scopes and grants.</p>
        </li>
        <li className="panel"><span className="guide-step">3</span><h2>Follow the recorded action</h2>
          <p>Keep the same intent reference when resubmitting the same request. Follow the returned operation ID.</p>
          <p className="hint">People with access can find its evidence in Actions. An unknown outcome is not permission to start a new request.</p>
        </li>
      </ol>
      <section className="table-wrap" aria-labelledby="mcp-tools-title">
        <div className="table-heading"><h2 id="mcp-tools-title">Available tools</h2><p className="muted">The existing local MCP interface</p></div>
        <table className="stack-table"><caption className="visually-hidden">MCP tools and what they allow</caption>
          <thead><tr><th scope="col">What it does</th><th scope="col">Tool</th><th scope="col">Purpose</th></tr></thead>
          <tbody>{TOOLS.map(([name, tool, purpose]) => <tr key={tool}><th scope="row">{name}</th><td data-label="Tool"><code>{tool}</code></td><td data-label="Purpose">{purpose}</td></tr>)}</tbody>
        </table>
      </section>
      <section className="notice info" aria-labelledby="mcp-boundary"><h2 id="mcp-boundary"><Icon name="lock" /> People approve restricted recovery</h2>
        <p>Agents cannot approve proposals, attest an effect was applied, change permissions, or force a retry. A separate authorized human reviewer decides, and every other safety rule still applies.</p>
      </section>
      <p className="context-note"><Icon name="box" />MCP supports individual actions. Start the full replacement workflow in the human console.</p>
    </div>
  </div>;
  return <><a className="skip-link" href="#main">Skip to main content</a><header className="guide-header"><Link to={principal ? "/actions" : "/login"} className="brand"><Brand /></Link><span className="demo-label">Local demo</span></header><main id="main" tabIndex={-1}>{content}</main></>;
}
