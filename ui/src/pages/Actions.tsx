import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";

import { api } from "../api/client";
import type { OperationPage, OperationSummary } from "../api/types";
import { Actor } from "../components/Actor";
import { Icon } from "../components/Icon";
import type { IconName } from "../components/Icon";
import { actionLabel, connectorLabel, resourceLabel } from "../components/labels";
import { usePageTitle } from "../components/pageTitle";
import { Empty, ErrorState, Loading, StateBadge, reasonText, stateInfo } from "../components/States";
import { COUNT_LIMIT, SummaryStrip, groupById, useStateCounts } from "../components/SummaryStrip";
import { RelativeTime } from "../components/time";
import { useSession } from "../session/SessionProvider";

const STATE_OPTIONS = [
  "RECORDED", "CLAIMED", "DISPATCHING", "UNKNOWN", "VERIFYING", "RETRY_ELIGIBLE",
  "ESCALATED_UNRESOLVED", "SUCCEEDED", "FAILED_CONFIRMED", "CANCELLED_BEFORE_CLAIM",
];
const CONNECTORS = ["mock-payments", "mock-crm", "mock-email", "stripe-test"];
const PAGE = 25;

const HOW: { icon: IconName; title: string; text: string }[] = [
  { icon: "file", title: "Recorded first", text: "Each action is saved before anything is sent to a provider." },
  { icon: "search", title: "Evidence before another attempt", text: "SafeAct checks uncertain outcomes. Another attempt must satisfy the provider's safety contract." },
  { icon: "user", title: "People decide restricted recovery", text: "MCP agents can request and investigate. A separate human reviewer decides proposals." },
];

function HowItWorks() {
  return (
    <details className="panel how">
      <summary>How SafeAct works</summary>
      <ol className="how__steps">
        {HOW.map((step) => (
          <li key={step.title}>
            <span className="icon-circle info"><Icon name={step.icon} /></span>
            <span><strong>{step.title}</strong><span className="muted">{step.text}</span></span>
          </li>
        ))}
      </ol>
    </details>
  );
}

export function Actions() {
  usePageTitle("Actions");
  const { principal, has } = useSession();
  const [params, setParams] = useSearchParams();
  const group = groupById(params.get("group") ?? "");
  // A one-state group is the server's own state filter, with its paging.
  const state = group?.states.length === 1 ? group.states[0]! : params.get("state") ?? "";
  const merged = group && group.states.length > 1 ? group : undefined;
  const connector = params.get("connector") ?? "";
  const unresolved = params.get("unresolved") === "true";
  const [cursors, setCursors] = useState<{ before: string; before_id: string }[]>([]);
  const [mergedPage, setMergedPage] = useState(0);
  const cursor = cursors.at(-1);
  const counts = useStateCounts(connector);

  const query = new URLSearchParams({ limit: String(PAGE) });
  if (state) query.set("state", state);
  if (connector) query.set("connector", connector);
  if (unresolved) query.set("unresolved", "true");
  if (cursor) {
    query.set("before", cursor.before);
    query.set("before_id", cursor.before_id);
  }

  const page = useQuery({
    queryKey: ["actions", query.toString()],
    queryFn: ({ signal }) => api<OperationPage>(`/actions?${query}`, { signal }),
    enabled: !merged,
  });

  // A group of several states lists what the counts already read: the
  // newest 100 of each state, merged newest first.
  const mergedPages = merged?.states.map((s) => counts[s]!) ?? [];
  const mergedError = mergedPages.find((q) => q.isError);
  const mergedItems: OperationSummary[] | undefined = merged && mergedPages.every((q) => q.data)
    ? mergedPages.flatMap((q) => q.data!.items).sort((a, b) =>
      b.updated_at.localeCompare(a.updated_at) || b.operation_id.localeCompare(a.operation_id))
    : undefined;
  const mergedTruncated = mergedPages.some((q) => q.data?.next_before);

  const items = merged
    ? mergedItems?.slice(mergedPage * PAGE, (mergedPage + 1) * PAGE)
    : page.data?.items;
  const pending = merged ? !mergedItems && !mergedError : page.isPending;
  const error = merged ? mergedError?.error : page.isError ? page.error : null;
  const retry = () => (merged ? mergedPages.forEach((q) => void q.refetch()) : void page.refetch());

  const update = (changes: Record<string, string>) => {
    const next = new URLSearchParams(params);
    for (const [name, value] of Object.entries(changes)) {
      if (value) next.set(name, value);
      else next.delete(name);
    }
    setParams(next, { replace: true });
    setCursors([]);
    setMergedPage(0);
  };
  const showReason = Boolean(items?.some((item) => item.unknown_reason));
  const filtered = Boolean(group || state || connector || unresolved);

  const hasNewer = merged ? mergedPage > 0 : cursors.length > 0;
  const hasOlder = merged
    ? Boolean(mergedItems && (mergedPage + 1) * PAGE < mergedItems.length)
    : Boolean(page.data?.next_before && page.data.next_before_id);

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Actions</h1>
          <p className="page-header__lede">
            See what happened and what needs attention, whether requested by a person or an AI agent.
          </p>
        </div>
        {has("requester") && <Link className="button" to="/submit"><Icon name="send" />New action</Link>}
      </div>
      <div className="stack">
        <SummaryStrip counts={counts} selected={group?.id ?? ""}
                      onSelect={(id) => update({ group: id, state: "", unresolved: "" })} />
        <p className="context-note"><Icon name="info" />An uncertain outcome is not a failure. Open an action to inspect its evidence and next safe step.</p>

        <form className="filters" role="search" aria-label="Filter actions" onSubmit={(e) => e.preventDefault()}>
          <div className="field">
            <label htmlFor="filter-state">Status</label>
            <select id="filter-state" value={group ? "" : state}
                    onChange={(e) => update({ state: e.target.value, group: "" })}>
              <option value="">{merged ? merged.label : group ? stateInfo(state).label : "Any status"}</option>
              {STATE_OPTIONS.map((s) => <option key={s} value={s}>{stateInfo(s).label}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="filter-connector">Service</label>
            <select id="filter-connector" value={connector} onChange={(e) => update({ connector: e.target.value })}>
              <option value="">Any service</option>
              {CONNECTORS.map((c) => <option key={c} value={c}>{connectorLabel(c)}</option>)}
            </select>
          </div>
          <label htmlFor="filter-unresolved" className="check">
            <input id="filter-unresolved" type="checkbox" checked={unresolved}
                   onChange={(e) => update({ unresolved: e.target.checked ? "true" : "", group: "" })} />
            Only unknown or needing review
          </label>
          {filtered && items?.length !== 0 && (
            <button type="button" className="ghost filters__clear" onClick={() => update({
              group: "", state: "", connector: "", unresolved: "" })}>Clear filters</button>
          )}
        </form>

        {merged && mergedTruncated && (
          <p className="hint">
            Showing the newest {COUNT_LIMIT} per state; choose a single state to see older ones.
          </p>
        )}
        {pending && <Loading label="Loading actions…" rows={5} />}
        {error && <ErrorState error={error} retry={retry} />}
        {items && items.length === 0 && (
          <Empty action={filtered
            ? <button type="button" className="secondary" onClick={() => update({
              group: "", state: "", connector: "", unresolved: "" })}>Clear filters</button>
            : undefined}>
            {filtered ? "No actions match these filters." : "No actions yet."}
            <span className="empty__hint muted">
              {filtered ? " Try another status or service." : " When someone submits an action, it appears here."}
            </span>
          </Empty>
        )}
        {items && items.length > 0 && (
          <div className="table-wrap">
            <table className="stack-table actions-table">
              <caption className="visually-hidden">Actions you can see, newest first</caption>
              <thead>
                <tr>
                  <th scope="col">Action</th>
                  <th scope="col">For</th>
                  <th scope="col">Requested by</th>
                  <th scope="col">Status</th>
                  {showReason && <th scope="col">Why unknown</th>}
                  <th scope="col">Last change</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.operation_id} className="row-link">
                    <td>
                      <Link className="row-link__target" to={`/actions/${item.operation_id}`}>
                        {actionLabel(item.action_type)}
                      </Link>
                    </td>
                    <td data-label="For">
                      <span>{resourceLabel(item.resource_id)}</span>
                      <span className="cell-sub">{connectorLabel(item.connector_id)}</span>
                    </td>
                    <td data-label="Requested by"><Actor id={item.requested_by} principal={principal} /></td>
                    <td data-label="Status" className="cell-status"><StateBadge state={item.state} /></td>
                    {showReason && (
                      <td data-label="Why unknown" className={item.unknown_reason ? undefined : "muted"}>
                        {item.unknown_reason ? reasonText(item.unknown_reason) : "—"}
                      </td>
                    )}
                    <td data-label="Last change" className="muted"><RelativeTime value={item.updated_at} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <HowItWorks />
        {(hasNewer || hasOlder) && (
          <div className="pager">
            {hasNewer && (
              <button type="button" className="secondary" onClick={() => (merged
                ? setMergedPage(mergedPage - 1) : setCursors(cursors.slice(0, -1)))}>
                Newer
              </button>
            )}
            {hasOlder && (
              <button type="button" className="secondary" onClick={() => (merged
                ? setMergedPage(mergedPage + 1)
                : setCursors([...cursors, { before: page.data!.next_before!, before_id: page.data!.next_before_id! }]))}>
                Older
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
