import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { QueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo } from "react";
import type { ReactNode } from "react";

import { ApiError, api, setUnauthenticatedHandler } from "../api/client";
import type { Principal, Role } from "../api/types";

interface Session {
  principal: Principal | null;
  loading: boolean;
  signIn: (username: string, password: string) => Promise<Principal>;
  signOut: () => Promise<void>;
  /** Cosmetic only: the API enforces every rule regardless. */
  has: (role: Role) => boolean;
}

const SessionContext = createContext<Session | undefined>(undefined);

export const ME = ["me"] as const;

/** Drop every cached view except the session itself, then set it. */
export function resetSession(client: QueryClient, principal: Principal | null) {
  client.removeQueries({ predicate: (query) => query.queryKey[0] !== ME[0] });
  client.setQueryData(ME, principal);
}

async function fetchMe(): Promise<Principal | null> {
  try {
    return await api<Principal>("/auth/me");
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return null;
    throw error;
  }
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient();
  const me = useQuery({ queryKey: ME, queryFn: fetchMe, staleTime: 60_000 });

  // Any 401 from the API means the session is gone (expired, revoked,
  // signed out elsewhere): drop every cached view and show sign-in.
  useEffect(() => {
    setUnauthenticatedHandler(() => resetSession(client, null));
    return () => setUnauthenticatedHandler(undefined);
  }, [client]);

  const signIn = useCallback(async (username: string, password: string) => {
    const result = await api<{ principal: Principal }>("/auth/login", {
      method: "POST",
      body: { username, password },
    });
    resetSession(client, result.principal);
    return result.principal;
  }, [client]);

  const signOut = useCallback(async () => {
    try {
      await api<void>("/auth/logout", { method: "POST" });
    } finally {
      resetSession(client, null);
    }
  }, [client]);

  const value = useMemo<Session>(() => ({
    principal: me.data ?? null,
    loading: me.isPending,
    signIn,
    signOut,
    has: (role) => me.data?.roles.includes(role) ?? false,
  }), [me.data, me.isPending, signIn, signOut]);

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession outside SessionProvider");
  return session;
}
