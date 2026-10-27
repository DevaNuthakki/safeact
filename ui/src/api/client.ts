// The console's only door to the API. Same origin (the dev server
// proxies to uvicorn), so the session cookie is first-party. Every write
// carries the CSRF token from its readable cookie; PostgreSQL checks it.
// Nothing here decides permissions: a refusal is the API's answer.

export const CSRF_COOKIE = "__Host-safeact_csrf";
export const CSRF_HEADER = "X-SafeAct-CSRF";

export interface ErrorBody {
  error: {
    code: string;
    message: string;
    request_id?: string;
    reason?: string;
    fields?: { loc: (string | number)[]; type: string }[];
    proposal?: unknown;
  };
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly reason: string | undefined;
  readonly body: ErrorBody | undefined;

  constructor(status: number, body: ErrorBody | undefined) {
    super(body?.error.message ?? `Request failed (${status}).`);
    this.name = "ApiError";
    this.status = status;
    this.code = body?.error.code ?? "http_error";
    this.reason = body?.error.reason;
    this.body = body;
  }
}

/** Raised for network failures: the request may or may not have arrived. */
export class NetworkError extends Error {
  constructor() {
    super("The service could not be reached. Check your connection and try again.");
    this.name = "NetworkError";
  }
}

let onUnauthenticated: (() => void) | undefined;

/** The session provider learns here when the API says the session ended. */
export function setUnauthenticatedHandler(handler: (() => void) | undefined) {
  onUnauthenticated = handler;
}

export function readCookie(name: string): string | undefined {
  for (const part of document.cookie.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key === name) return decodeURIComponent(rest.join("="));
  }
  return undefined;
}

type Method = "GET" | "POST";

export async function api<T>(
  path: string,
  options: { method?: Method; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  const method = options.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") {
    const token = readCookie(CSRF_COOKIE);
    if (token) headers[CSRF_HEADER] = token;
  }

  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new NetworkError();
  }

  if (response.status === 204) return undefined as T;
  const text = await response.text();
  const data: unknown = text ? JSON.parse(text) : undefined;
  if (!response.ok) {
    const body = isErrorBody(data) ? data : undefined;
    if (response.status === 401 && path !== "/auth/login") onUnauthenticated?.();
    throw new ApiError(response.status, body);
  }
  return data as T;
}

function isErrorBody(value: unknown): value is ErrorBody {
  return (
    typeof value === "object" && value !== null && "error" in value &&
    typeof (value as ErrorBody).error?.code === "string"
  );
}

/** A request key minted once per form, so a resubmit is the same operation. */
export function newRequestKey(): string {
  return "ui-" + crypto.randomUUID();
}
