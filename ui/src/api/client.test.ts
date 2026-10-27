import { ApiError, CSRF_HEADER, NetworkError, api, setUnauthenticatedHandler } from "./client";
import { mockFetch } from "../test/utils";

describe("api client", () => {
  beforeEach(() => {
    // jsdom rightly refuses a __Host- cookie on a non-secure origin, so
    // stand in for the browser's cookie jar.
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "other=1; __Host-safeact_csrf=tok123",
    );
  });

  it("sends the CSRF token on writes only", async () => {
    const calls = mockFetch(() => ({ status: 200, body: { ok: true } }));
    await api("/actions");
    await api("/actions", { method: "POST", body: { a: 1 } });
    const headers = calls.map((c) => new Headers(c.init.headers as Record<string, string>));
    expect(headers[0]!.get(CSRF_HEADER)).toBeNull();
    expect(headers[1]!.get(CSRF_HEADER)).toBe("tok123");
    expect(calls[1]!.init.credentials).toBe("same-origin");
    expect(calls[1]!.init.body).toBe('{"a":1}');
  });

  it("turns error bodies into typed errors", async () => {
    mockFetch(() => ({ status: 409, body: { error: {
      code: "stale_approval", message: "Changed.", reason: "evidence_changed",
    } } }));
    const error = await api("/proposals/x/decision", { method: "POST", body: {} })
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("stale_approval");
    expect((error as ApiError).reason).toBe("evidence_changed");
  });

  it("reports a dead session once and not for a failed sign-in", async () => {
    const ended = vi.fn();
    setUnauthenticatedHandler(ended);
    mockFetch(() => ({ status: 401, body: { error: { code: "unauthenticated", message: "x" } } }));
    await api("/actions").catch(() => undefined);
    await api("/auth/login", { method: "POST", body: {} }).catch(() => undefined);
    expect(ended).toHaveBeenCalledTimes(1);
    setUnauthenticatedHandler(undefined);
  });

  it("separates network failures from refusals", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("failed"); }));
    await expect(api("/actions")).rejects.toBeInstanceOf(NetworkError);
  });
});
