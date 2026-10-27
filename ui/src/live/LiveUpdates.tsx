import { useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import { ME, resetSession } from "../session/SessionProvider";

// Live updates over SSE (GET /events). Events only say what changed;
// the views re-read over REST, so the database stays the source of
// truth. The browser's EventSource reconnects by itself and resends
// Last-Event-ID; when it gives up (for example the API restarted and
// refused once), we start a new stream with ?after=<last id> after a
// growing delay, then re-read everything once to be sure.

export type LiveStatus = "connecting" | "live" | "reconnecting" | "offline";

const LiveContext = createContext<LiveStatus>("connecting");

const MAX_DELAY_MS = 30_000;
/** The summary counts read ten pages; refresh them at most this often. */
export const COUNTS_INTERVAL_MS = 2_000;

interface Options {
  enabled: boolean;
  /** Test seam: the EventSource implementation. */
  source?: typeof EventSource;
  baseDelayMs?: number;
}

export function LiveUpdates({ children, enabled, source, baseDelayMs = 1000 }: Options & { children: ReactNode }) {
  const status = useLiveUpdates({ enabled, source, baseDelayMs });
  return <LiveContext.Provider value={status}>{children}</LiveContext.Provider>;
}

export function useLiveStatus(): LiveStatus {
  return useContext(LiveContext);
}

interface Change {
  operation_id: string;
  events: string[];
}

export function useLiveUpdates({ enabled, source, baseDelayMs = 1000 }: Options): LiveStatus {
  const client = useQueryClient();
  const [status, setStatus] = useState<LiveStatus>("connecting");
  const lastId = useRef<string | null>(null);

  useEffect(() => {
    if (!enabled) return undefined;
    const Source = source ?? EventSource;
    let stream: EventSource | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    let stopped = false;
    let recovering = false;
    let countsTimer: ReturnType<typeof setTimeout> | undefined;
    let countsAt = 0;

    // Leading and trailing: the first change refreshes the counts at
    // once, later ones within the interval are folded into one refresh.
    const refreshCounts = () => {
      const wait = countsAt + COUNTS_INTERVAL_MS - Date.now();
      const run = () => {
        countsTimer = undefined;
        countsAt = Date.now();
        void client.invalidateQueries({ queryKey: ["action-counts"] });
      };
      if (wait <= 0 && !countsTimer) run();
      else countsTimer ??= setTimeout(run, Math.max(0, wait));
    };

    const rereadAll = () => client.invalidateQueries({
      predicate: (query) => query.queryKey[0] !== ME[0],
    });

    const open = () => {
      const url = lastId.current ? `/events?after=${encodeURIComponent(lastId.current)}` : "/events";
      stream = new Source(url);

      stream.onopen = () => {
        failures = 0;
        setStatus("live");
        if (recovering) {
          recovering = false;
          void rereadAll();
        }
      };

      const remember = (event: MessageEvent) => {
        if (event.lastEventId) lastId.current = event.lastEventId;
      };

      stream.addEventListener("reset", (event) => {
        remember(event as MessageEvent);
        void rereadAll();
      });

      stream.addEventListener("changes", (event) => {
        const message = event as MessageEvent<string>;
        remember(message);
        const { changes } = JSON.parse(message.data) as { changes: Change[] };
        void client.invalidateQueries({ queryKey: ["actions"] });
        void client.invalidateQueries({ queryKey: ["proposals"] });
        void client.invalidateQueries({ queryKey: ["workflow"] });
        refreshCounts();
        for (const change of changes) {
          void client.invalidateQueries({ queryKey: ["action", change.operation_id] });
        }
      });

      stream.addEventListener("reconnect", (event) => remember(event as MessageEvent));

      stream.addEventListener("session_ended", () => {
        stopped = true;
        stream?.close();
        setStatus("offline");
        resetSession(client, null);
      });

      stream.onerror = () => {
        if (stopped) return;
        if (stream && stream.readyState === Source.CONNECTING) {
          // The browser is retrying with Last-Event-ID itself.
          setStatus("reconnecting");
          recovering = true;
          return;
        }
        stream?.close();
        failures += 1;
        recovering = true;
        setStatus(failures > 3 ? "offline" : "reconnecting");
        const delay = Math.min(MAX_DELAY_MS, baseDelayMs * 2 ** (failures - 1));
        timer = setTimeout(open, delay);
      };
    };

    open();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      if (countsTimer) clearTimeout(countsTimer);
      stream?.close();
    };
  }, [enabled, source, baseDelayMs, client]);

  return enabled ? status : "offline";
}
