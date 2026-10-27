import { newRequestKey } from "./client";

export interface PendingAction {
  request_key: string;
  action_type: string;
  provider_account_id: string;
  contract_version: string;
  payload: Record<string, unknown>;
}

const keyStorage = (owner: string) => `safeact.submit.requestKey.${owner}`;
export const actionStorage = (owner: string) => `safeact.submit.pending.${owner}`;

// Retain identity per person and tenant, never across different sign-ins.
export function currentRequestKey(owner: string): string {
  try {
    const existing = sessionStorage.getItem(keyStorage(owner));
    if (existing) return existing;
    const key = newRequestKey();
    sessionStorage.setItem(keyStorage(owner), key);
    return key;
  } catch {
    // The form may be displayed, but rememberAction must succeed before POST.
    return newRequestKey();
  }
}

export function pendingAction(owner: string): PendingAction | null {
  const stored = sessionStorage.getItem(actionStorage(owner));
  if (!stored) return null;
  const value: unknown = JSON.parse(stored);
  if (!value || typeof value !== "object") throw new Error("Invalid retained request");
  const body = value as Partial<PendingAction>;
  if (typeof body.request_key !== "string" || !body.request_key
      || typeof body.action_type !== "string" || !body.action_type
      || typeof body.provider_account_id !== "string" || !body.provider_account_id
      || typeof body.contract_version !== "string" || !body.contract_version
      || !body.payload || typeof body.payload !== "object" || Array.isArray(body.payload)) {
    throw new Error("Invalid retained request");
  }
  return body as PendingAction;
}

export function rememberAction(owner: string, body: PendingAction) {
  const existing = pendingAction(owner);
  if (existing && JSON.stringify(existing) !== JSON.stringify(body)) {
    throw new Error("An earlier submission must be recovered first");
  }
  sessionStorage.setItem(actionStorage(owner), JSON.stringify(body));
}

/** Clear only the request whose matching server record is now on screen. */
export function forgetRequestKey(submitted: string, owner: string) {
  try {
    if (pendingAction(owner)?.request_key === submitted) sessionStorage.removeItem(actionStorage(owner));
    if (sessionStorage.getItem(keyStorage(owner)) === submitted) sessionStorage.removeItem(keyStorage(owner));
  } catch {
    // Failure to clean up must preserve the intent, never silently replace it.
  }
}
