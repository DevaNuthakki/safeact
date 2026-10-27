import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";

import { LiveUpdates, useLiveStatus } from "./LiveUpdates";
import { testClient } from "../test/utils";

class FakeSource {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  static instances: FakeSource[] = [];
  readyState = FakeSource.CONNECTING;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, ((e: MessageEvent) => void)[]>();
  closed = false;

  constructor(public url: string) {
    FakeSource.instances.push(this);
  }
  addEventListener(name: string, fn: (e: MessageEvent) => void) {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), fn]);
  }
  close() {
    this.closed = true;
    this.readyState = FakeSource.CLOSED;
  }
  open() {
    this.readyState = FakeSource.OPEN;
    this.onopen?.();
  }
  emit(name: string, data: unknown, id = "") {
    const event = new MessageEvent(name, { data: JSON.stringify(data), lastEventId: id });
    for (const fn of this.listeners.get(name) ?? []) fn(event);
  }
  fail(readyState: number) {
    this.readyState = readyState;
    this.onerror?.();
  }
}

function Status() {
  return <p>status: {useLiveStatus()}</p>;
}

function setup() {
  FakeSource.instances = [];
  const client = testClient();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  render(
    <QueryClientProvider client={client}>
      <LiveUpdates enabled source={FakeSource as unknown as typeof EventSource} baseDelayMs={10}>
        <Status />
      </LiveUpdates>
    </QueryClientProvider>,
  );
  return { client, invalidate, source: () => FakeSource.instances.at(-1)! };
}

describe("live updates", () => {
  it("re-reads the views named by a changes event", () => {
    const { invalidate, source } = setup();
    act(() => source().open());
    expect(screen.getByText("status: live")).toBeInTheDocument();
    act(() => source().emit("changes", { changes: [{ operation_id: "op-1", events: [] }] }, "42"));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["action", "op-1"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["actions"] });
  });

  it("refreshes the summary counts at most once every two seconds", () => {
    vi.useFakeTimers();
    try {
      const { invalidate, source } = setup();
      act(() => source().open());
      const counts = () => invalidate.mock.calls.filter(([f]) =>
        JSON.stringify(f) === JSON.stringify({ queryKey: ["action-counts"] })).length;
      const change = () => act(() => source().emit("changes", { changes: [] }));
      change();
      expect(counts()).toBe(1);
      change();
      change();
      expect(counts()).toBe(1);
      act(() => { vi.advanceTimersByTime(1_999); });
      expect(counts()).toBe(1);
      act(() => { vi.advanceTimersByTime(1); });
      expect(counts()).toBe(2);
      act(() => { vi.advanceTimersByTime(5_000); });
      expect(counts()).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("re-reads everything on reset", () => {
    const { invalidate, source } = setup();
    act(() => source().open());
    act(() => source().emit("reset", { truncated: false }, "7"));
    expect(invalidate).toHaveBeenCalledWith(expect.objectContaining({ predicate: expect.any(Function) }));
  });

  it("restarts from the last event id after the stream gives up", async () => {
    vi.useFakeTimers();
    try {
      const { source, invalidate } = setup();
      act(() => source().open());
      act(() => source().emit("changes", { changes: [] }, "99"));
      act(() => source().fail(FakeSource.CLOSED));
      expect(screen.getByText("status: reconnecting")).toBeInTheDocument();
      await act(async () => { vi.advanceTimersByTime(20); });
      expect(source().url).toBe("/events?after=99");
      invalidate.mockClear();
      act(() => source().open());
      // After an outage the views are read again once.
      expect(invalidate).toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("lets the browser retry by itself while it is connecting", () => {
    const { source } = setup();
    act(() => source().open());
    const before = FakeSource.instances.length;
    act(() => source().fail(FakeSource.CONNECTING));
    expect(screen.getByText("status: reconnecting")).toBeInTheDocument();
    expect(FakeSource.instances.length).toBe(before);
  });

  it("stops and signs out when the session ends", () => {
    const { client, source } = setup();
    act(() => source().open());
    act(() => source().emit("session_ended", {}));
    expect(source().closed).toBe(true);
    expect(client.getQueryData(["me"])).toBeNull();
    expect(screen.getByText("status: offline")).toBeInTheDocument();
  });
});
