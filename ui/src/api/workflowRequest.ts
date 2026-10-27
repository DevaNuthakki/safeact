import type { CreateWorkflow } from "./types";

const STORAGE = "safeact.workflow.pending";

// Save the entire intent before POST, so reloading after a lost admission
// reply cannot silently change the action or generate a second workflow.
export function pendingWorkflow(owner: string): CreateWorkflow | null {
  try {
    const value = sessionStorage.getItem(`${STORAGE}.${owner}`);
    return value ? JSON.parse(value) as CreateWorkflow : null;
  } catch {
    return null;
  }
}

export function rememberWorkflow(owner: string, body: CreateWorkflow) {
  sessionStorage.setItem(`${STORAGE}.${owner}`, JSON.stringify(body));
}

export function forgetWorkflow(owner: string, requestKey: string) {
  if (pendingWorkflow(owner)?.request_key === requestKey) {
    sessionStorage.removeItem(`${STORAGE}.${owner}`);
  }
}
