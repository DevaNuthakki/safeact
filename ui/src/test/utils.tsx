import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";

import { SessionProvider } from "../session/SessionProvider";

export function testClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

type Entry = string | { pathname: string; search?: string; state?: unknown };

export function renderWith(
  ui: ReactElement, { route = "/" as Entry, client = testClient() } = {},
) {
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[route]}>
          <SessionProvider>{ui}</SessionProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  };
}

type Handler = (url: string, init: RequestInit) => { status: number; body?: unknown };

/** Replace fetch with a handler; records every call. */
export function mockFetch(handler: Handler) {
  const calls: { url: string; init: RequestInit }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url, init });
    const { status, body } = handler(url, init);
    return new Response(body === undefined ? null : JSON.stringify(body), {
      status, headers: { "Content-Type": "application/json" },
    });
  }));
  return calls;
}

export const PRINCIPAL = {
  principal_id: "p-1", tenant_id: "tenant-north", username: "demo-north-requester",
  display_name: "North Requester", kind: "human", roles: ["requester"], demo: true,
  session_expires_at: "2026-10-19T20:00:00Z",
};
