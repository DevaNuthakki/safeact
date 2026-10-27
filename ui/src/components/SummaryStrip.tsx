import { useQueries } from "@tanstack/react-query";

import { api } from "../api/client";
import type { OperationPage } from "../api/types";
import { Icon } from "./Icon";
import type { IconName } from "./Icon";

// Counts by state without a counts endpoint: one page of up to 100 per
// state, so a count is exact below 100 and "100+" beyond. Live changes
// refresh them at most once every two seconds (LiveUpdates).

export const COUNT_LIMIT = 100;

export const GROUPS: {
  id: string; label: string; help: string; tone: string; icon: IconName; states: string[];
}[] = [
  { id: "in_progress", label: "In progress", help: "Recorded or being processed", tone: "info", icon: "clock",
    states: ["RECORDED", "CLAIMED", "DISPATCHING", "RETRY_ELIGIBLE"] },
  { id: "checking", label: "Checking", help: "Asking the provider", tone: "warn", icon: "search",
    states: ["UNKNOWN", "VERIFYING"] },
  { id: "needs_review", label: "Needs review", help: "A person should look", tone: "warn", icon: "alert",
    states: ["ESCALATED_UNRESOLVED"] },
  { id: "succeeded", label: "Succeeded", help: "Safely completed", tone: "ok", icon: "check", states: ["SUCCEEDED"] },
  { id: "failed", label: "Failed or cancelled", help: "Not done or stopped", tone: "bad", icon: "x",
    states: ["FAILED_CONFIRMED", "CANCELLED_BEFORE_CLAIM"] },
];

export function groupById(id: string) {
  return GROUPS.find((g) => g.id === id);
}

/** One query per state; shared by the strip and by a group's list. */
export function useStateCounts(connector: string) {
  const states = GROUPS.flatMap((g) => g.states);
  const results = useQueries({
    queries: states.map((state) => ({
      queryKey: ["action-counts", state, connector],
      queryFn: ({ signal }: { signal: AbortSignal }) => {
        const query = new URLSearchParams({ state, limit: String(COUNT_LIMIT) });
        if (connector) query.set("connector", connector);
        return api<OperationPage>(`/actions?${query}`, { signal });
      },
    })),
  });
  return Object.fromEntries(states.map((state, i) => [state, results[i]!]));
}

export type StateCounts = ReturnType<typeof useStateCounts>;

export function groupCount(counts: StateCounts, states: string[]) {
  const pages = states.map((s) => counts[s]?.data);
  if (pages.some((p) => !p)) return null;
  const total = pages.reduce((n, p) => n + p!.items.length, 0);
  const more = pages.some((p) => Boolean(p!.next_before));
  return { total, more, text: more ? `${total}+` : String(total) };
}

export function SummaryStrip({ counts, selected, onSelect }: {
  counts: StateCounts; selected: string; onSelect: (group: string) => void;
}) {
  return (
    <section aria-label="Counts by state">
      <p className="summary__caption">All actions you can see. Choose a tile to filter the list.</p>
      <ul className="summary">
        {GROUPS.map((group) => {
          const failed = group.states.some((state) => counts[state]?.isError);
          const count = failed ? null : groupCount(counts, group.states);
          const pressed = selected === group.id;
          return (
            <li key={group.id}>
              <button type="button" aria-pressed={pressed}
                      className={`tile ${group.tone}${group.id === "needs_review" && count && count.total > 0 ? " tile--emphasis" : ""}`}
                      onClick={() => {
                        for (const state of group.states) if (counts[state]?.isError) void counts[state]!.refetch();
                        onSelect(pressed ? "" : group.id);
                      }}>
                <span className={`icon-circle icon-circle--lg ${group.tone}`}><Icon name={group.icon} /></span>
                <span className="tile__body">
                  <span className="tile__label">{group.label}</span>{" "}
                  <span className="tile__count">{count ? count.text : "–"}</span>{" "}
                  {!count && <span className="visually-hidden">{failed ? "count unavailable " : "loading "}</span>}
                  <span className="tile__help">{failed ? "Count unavailable; open to retry" : group.help}</span>
                </span>
                <span className="tile__selected" aria-hidden="true"><Icon name="check" /></span>
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
