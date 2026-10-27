import type { Grant } from "./types";

/** Preserve an exact selection when permissions refresh or change order. */
export function grantKey(grant: Grant) {
  return JSON.stringify([grant.connector_id, grant.provider_account_id,
    grant.action_type, grant.resource_id, grant.contract_version]);
}
