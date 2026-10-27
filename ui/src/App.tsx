import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Link, Navigate, Outlet, Route, Routes, useLocation } from "react-router";

import { ApiError } from "./api/client";
import { Layout } from "./components/Layout";
import { RequireReviewer, WalkthroughLayout } from "./components/WalkthroughLayout";
import { usePageTitle } from "./components/pageTitle";
import { Empty, Loading } from "./components/States";
import { LiveUpdates } from "./live/LiveUpdates";
import { ActionDetail } from "./pages/ActionDetail";
import { Actions } from "./pages/Actions";
import { Login } from "./pages/Login";
import { McpGuide } from "./pages/McpGuide";
import { ProposalDetail } from "./pages/ProposalDetail";
import { Proposals } from "./pages/Proposals";
import { Submit } from "./pages/Submit";
import { WorkflowDetail } from "./pages/WorkflowDetail";
import { WorkflowSubmit } from "./pages/WorkflowSubmit";
import { WalkthroughStart } from "./pages/WalkthroughStart";
import { AgentChapter, RefundChapter } from "./pages/WalkthroughCases";
import { DecisionChapter } from "./pages/WalkthroughDecision";
import { ResultsChapter, WalkthroughEvidence } from "./pages/WalkthroughResults";
import { SessionProvider, useSession } from "./session/SessionProvider";
import "./styles/walkthrough.css";

export function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // Live updates invalidate views; this is only a backstop.
        staleTime: 5_000,
        refetchOnWindowFocus: true,
        retry: (count, error) =>
          !(error instanceof ApiError && error.status < 500) && count < 2,
      },
      mutations: { retry: false },
    },
  });
}

function NotFound() {
  usePageTitle("Page not found");
  return (
    <div className="page page--detail">
      <div className="page-header"><h1>Page not found</h1></div>
      <Empty action={<p><Link to="/actions">Go to actions</Link></p>}>
        We couldn't find that page.
        <span className="empty__hint muted"> The link may be old, or this page belongs to another account.</span>
      </Empty>
    </div>
  );
}

function RequireSession() {
  const { principal, loading } = useSession();
  const location = useLocation();
  if (loading) return <main id="main"><div className="page"><Loading label="Checking your session…" /></div></main>;
  if (!principal) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return (
    <LiveUpdates enabled>
      <Outlet />
    </LiveUpdates>
  );
}

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/mcp-guide" element={<McpGuide />} />
      <Route path="/" element={<Navigate to="/demo" replace />} />
      <Route element={<WalkthroughLayout />}>
        <Route path="/demo" element={<WalkthroughStart />} />
        <Route path="/demo/results" element={<ResultsChapter />} />
        <Route path="/demo/evidence/:document" element={<WalkthroughEvidence />} />
        <Route element={<RequireReviewer />}>
          <Route path="/demo/refund" element={<RefundChapter />} />
          <Route path="/demo/agent" element={<AgentChapter />} />
          <Route path="/demo/decision" element={<DecisionChapter />} />
        </Route>
      </Route>
      <Route element={<RequireSession />}>
        <Route element={<Layout />}>
          <Route path="/actions" element={<Actions />} />
          <Route path="/actions/:operationId" element={<ActionDetail />} />
          <Route path="/submit" element={<Submit />} />
          <Route path="/workflows/new" element={<WorkflowSubmit />} />
          <Route path="/workflows/:workflowId" element={<WorkflowDetail />} />
          <Route path="/proposals" element={<Proposals />} />
          <Route path="/proposals/:proposalId" element={<ProposalDetail />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Route>
    </Routes>
  );
}

export function App({ client = makeQueryClient() }: { client?: QueryClient }) {
  return (
    <QueryClientProvider client={client}>
      <SessionProvider>
        <AppRoutes />
      </SessionProvider>
    </QueryClientProvider>
  );
}
