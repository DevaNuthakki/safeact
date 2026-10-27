import type { Principal } from "../api/types";
import { COMPANIES, PEOPLE } from "../demo/directory";

// Names for principal ids. The signed-in person comes from the session;
// others from the demo directory (see demo/directory.ts). The raw id
// stays in Technical details.

const ROLE_TITLES: Record<string, string> = {
  requester: "Support agent",
  investigator: "Investigator",
  reviewer: "Reviewer",
};

export function personName(id: string | null | undefined, me?: Principal | null) {
  if (!id) return "Not recorded";
  if (me && id === me.principal_id) return `${me.display_name} (you)`;
  return PEOPLE[id]?.name ?? "Someone else on your team";
}

export function personTitle(principal: Principal) {
  return PEOPLE[principal.principal_id]?.title
    ?? principal.roles.map((r) => ROLE_TITLES[r] ?? r).join(", ");
}

export function companyName(tenantId: string) {
  return COMPANIES[tenantId] ?? tenantId;
}

export function initials(name: string) {
  const words = name.split(/\s+/).filter((w) => /^[A-Za-z]/.test(w));
  return ((words[0]?.[0] ?? "") + (words.length > 1 ? words.at(-1)![0] : "")).toUpperCase() || "?";
}
