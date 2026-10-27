import type { Principal } from "../api/types";
import { PEOPLE } from "../demo/directory";
import { Icon } from "./Icon";
import { personName } from "./people";

/** Attribution only. Identity and permissions are still decided by the API. */
export function Actor({ id, principal }: { id: string | null | undefined; principal?: Principal | null }) {
  const known = id ? PEOPLE[id] : undefined;
  const kind = id && id === principal?.principal_id
    ? principal.kind
    : known ? (known.title === "AI agent" ? "agent" : "human") : undefined;
  const agent = kind === "agent";
  return (
    <span className="actor">
      <Icon name={agent ? "robot" : kind === "human" ? "user" : "info"} />
      <span>
        <span className="actor__name">{personName(id, principal)}</span>
        <span className="actor__kind">{agent ? "AI agent · MCP" : kind === "human" ? "Person" : "Identity type not available"}</span>
      </span>
    </span>
  );
}
