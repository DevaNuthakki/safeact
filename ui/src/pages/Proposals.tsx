import { useQueries, useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router";

import { api } from "../api/client";
import type { OperationDetail, Proposal, ProposalStatus } from "../api/types";
import { Actor } from "../components/Actor";
import type { IconName } from "../components/Icon";
import { operationName, proposalTitle } from "../components/labels";
import { usePageTitle } from "../components/pageTitle";
import { personName } from "../components/people";
import { Badge, Empty, ErrorState, Loading } from "../components/States";
import { RelativeTime } from "../components/time";
import { useSession } from "../session/SessionProvider";
import { ACTION_LABEL } from "./ActionDetail";

const STATUSES: (ProposalStatus | "all")[] = ["open", "stale", "expired", "approved", "rejected", "all"];

// Red is kept for failed actions: a rejected proposal is a decision.
export const STATUS: Record<ProposalStatus, { label: string; tone: string; icon: IconName }> = {
  open: { label: "Open", tone: "info", icon: "clock" },
  stale: { label: "Stale", tone: "warn", icon: "alert" },
  expired: { label: "Expired", tone: "neutral", icon: "minus" },
  approved: { label: "Approved", tone: "ok", icon: "check" },
  rejected: { label: "Rejected", tone: "neutral", icon: "x" },
};

export function ProposalStatusBadge({ status }: { status: ProposalStatus }) {
  const info = STATUS[status];
  return <Badge tone={info.tone} icon={info.icon}>{info.label}</Badge>;
}

const EMPTY: Record<string, { title: string; hint: string }> = {
  open: { title: "Nothing waiting for review.",
    hint: "A review appears here once an investigator or an AI agent proposes a next step. "
      + "An action can need review before anyone has proposed one: those are listed under Actions." },
  stale: { title: "No stale reviews.", hint: "A review goes stale when its action changes before a decision." },
  expired: { title: "No expired reviews.", hint: "Reviews expire if nobody decides in time." },
  approved: { title: "No approved reviews yet.", hint: "Approved proposals are kept here for the record." },
  rejected: { title: "No rejected reviews.", hint: "Rejected proposals are kept here for the record." },
  all: { title: "No reviews yet.", hint: "Proposals from investigators and AI agents appear here." },
};

export function Proposals() {
  usePageTitle("Reviews");
  const { principal } = useSession();
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "open";
  const list = useQuery({
    queryKey: ["proposals", "list", status],
    queryFn: ({ signal }) => api<{ items: Proposal[] }>(`/proposals?status=${status}`, { signal }),
  });
  // Each review is titled by what it would settle, so read its action.
  const ids = [...new Set((list.data?.items ?? []).map((p) => p.operation_id))];
  const operations = useQueries({ queries: ids.map((id) => ({
    queryKey: ["action", id],
    queryFn: ({ signal }: { signal: AbortSignal }) => api<OperationDetail>(`/actions/${id}`, { signal }),
  })) });
  const byId = Object.fromEntries(ids.map((id, i) => [id, operations[i]?.data]));
  const empty = EMPTY[status] ?? EMPTY.all!;

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Reviews</h1>
          <p className="page-header__lede">
            Decide the next safe step using the current evidence. People and AI agents may propose; a separate human reviewer decides.
          </p>
        </div>
      </div>
      <div className="stack">
        <fieldset className="segmented">
          <legend className="visually-hidden">Show</legend>
          {STATUSES.map((s) => (
            <label key={s}>
              <input type="radio" name="proposal-status" value={s} checked={status === s}
                     onChange={() => setParams({ status: s }, { replace: true })} />
              <span>{s === "all" ? "All" : STATUS[s].label}</span>
            </label>
          ))}
        </fieldset>
        {list.isPending && <Loading label="Loading reviews…" rows={3} />}
        {list.isError && <ErrorState error={list.error} retry={() => list.refetch()} />}
        {list.data && list.data.items.length === 0 && (
          <Empty action={status === "open" ? (
            <p className="row">
              <Link to="/actions?group=needs_review">Actions that need review</Link>
              <Link to="/proposals?status=approved">Approved reviews</Link>
            </p>
          ) : undefined}>{empty.title}<span className="empty__hint muted"> {empty.hint}</span></Empty>
        )}
        {list.data && list.data.items.length > 0 && (
          <ul className="cards" aria-label="Reviews, newest first">
            {list.data.items.map((p) => {
              const op = byId[p.operation_id];
              return (
                <li key={p.proposal_id} className="panel card-link">
                  <div className="card-link__main">
                    <Link className="card-link__title" to={`/proposals/${p.proposal_id}`}>
                      {proposalTitle(p.action, op) ?? ACTION_LABEL[p.action]}
                    </Link>
                    <p className="card-link__meta">
                      <span>{op ? operationName(op) : "Loading the action…"}</span>
                      <span><RelativeTime value={p.created_at} /></span>
                    </p>
                    <div className="proposal-attribution"><span className="muted">Proposed by</span><Actor id={p.proposed_by} principal={principal} /></div>
                    {op && <p className="hint">Requested by {personName(op.requested_by, principal)}</p>}
                  </div>
                  <div className="card-link__side">
                    {p.status === "open" && <span>Expires <RelativeTime value={p.expires_at} /></span>}
                    <ProposalStatusBadge status={p.status} />
                    <span className="card-link__cta" aria-hidden="true">Review proposal →</span>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
