import type { IconName } from "./Icon";

// Human-readable names for action types. The raw type stays in the
// action's Technical details.

const ACTIONS: Record<string, string> = {
  refund_payment: "Refund payment",
  refund_stripe_test_payment: "Refund Stripe test payment",
  create_replacement_order: "Create replacement order",
  send_refund_confirmation: "Send refund confirmation",
  charge_shipping_fee: "Charge shipping fee",
};

const ICONS: Record<string, IconName> = {
  refund_payment: "refund",
  refund_stripe_test_payment: "refund",
  create_replacement_order: "box",
  send_refund_confirmation: "mail",
  charge_shipping_fee: "card",
};

export function actionIcon(actionType: string): IconName {
  return ICONS[actionType] ?? "file";
}

export function actionLabel(actionType: string) {
  if (ACTIONS[actionType]) return ACTIONS[actionType];
  const words = actionType.replaceAll("_", " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** "may_have_dispatched" -> "May have dispatched". */
export function humanize(code: string) {
  const words = code.replaceAll("_", " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// ---- Friendly names. Raw identifiers stay in Technical details. ----

const RESOURCES: [RegExp, string][] = [
  [/^ch_(?:.*_)?(\d+)$/, "Charge"],
  [/^case_(?:.*_)?(\d+)$/, "Support case"],
  [/^cust_(?:.*_)?(\d+)$/, "Customer"],
];

/** "ch_north_1002" -> "Charge #1002"; unknown ids are shown as they are. */
export function resourceLabel(id: string) {
  for (const [pattern, noun] of RESOURCES) {
    const match = pattern.exec(id);
    if (match) return `${noun} #${match[1]}`;
  }
  if (/^pi_[A-Za-z0-9]{4,}$/.test(id)) return `Stripe test payment ···${id.slice(-4)}`;
  return id;
}

const CONNECTORS: Record<string, string> = {
  "mock-payments": "Payments (test)",
  "mock-crm": "CRM (test)",
  "mock-email": "Email (test)",
  "stripe-test": "Stripe (test mode)",
};

export function connectorLabel(id: string) {
  return CONNECTORS[id] ?? id;
}

// Every status Stripe documents for a Refund, plus the adapter's own
// "unreported" (malformed or missing). Anything else is shown verbatim.
const REFUND_STATUSES: Record<string, string> = {
  pending: "Pending",
  requires_action: "Requires action",
  succeeded: "Succeeded",
  failed: "Failed",
  canceled: "Canceled",
  unreported: "Not reported",
};

export function refundStatusLabel(status: string) {
  return REFUND_STATUSES[status] ?? status;
}

/**
 * The refund status the provider reported in its response, recorded as
 * details.provider.refund_status (Stripe only). A snapshot from that
 * response, not a live value; SafeAct never decides anything from it.
 */
export function reportedRefundStatus(details: Record<string, unknown> | undefined): string | null {
  const provider = details?.provider;
  if (!provider || typeof provider !== "object") return null;
  const status = (provider as Record<string, unknown>).refund_status;
  return typeof status === "string" ? status : null;
}

/** The most recently recorded refund status across every attempt, or null. */
export function latestRefundStatus(attempts: { outcomes: { details: Record<string, unknown> }[] }[]) {
  let found: string | null = null;
  for (const attempt of attempts) {
    for (const row of attempt.outcomes) found = reportedRefundStatus(row.details) ?? found;
  }
  return found;
}

/** Integer cents as US dollars, "$1,234.05", with integer arithmetic only. */
export function money(cents: number) {
  if (!Number.isSafeInteger(cents) || cents < 0) return "an invalid amount";
  const rest = cents % 100;
  const whole = (cents - rest) / 100;
  return `$${whole.toLocaleString("en-US")}.${String(rest).padStart(2, "0")}`;
}

/** Most the API accepts for one amount: 10,000,000 cents ($100,000.00). */
export const MAX_CENTS = 10_000_000n;

/**
 * "15.5" -> 1550. Parses the text exactly (no floating point): digits with
 * at most two decimals, more than zero and at most MAX_CENTS. Anything
 * else is null.
 */
export function dollarsToCents(text: string): number | null {
  const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(text.trim());
  if (!match) return null;
  const cents = BigInt(match[1]!) * 100n + BigInt((match[2] ?? "").padEnd(2, "0"));
  return cents > 0n && cents <= MAX_CENTS ? Number(cents) : null;
}

/** Cents back to the text a person typed, "1550" -> "15.50". */
export function centsToDollars(cents: number) {
  return money(cents).slice(1).replaceAll(",", "");
}

function amountOf(payload: Record<string, unknown> | undefined) {
  const value = payload?.amount_minor;
  return typeof value === "number" ? money(value) : null;
}

/** "Refund $2,450.00 on Charge #1002": what an operation does, in words. */
export function operationName(op: {
  action_type: string; resource_id: string; payload?: Record<string, unknown>;
}) {
  const what = resourceLabel(op.resource_id);
  const amount = amountOf(op.payload);
  switch (op.action_type) {
    case "refund_payment":
      return amount ? `Refund ${amount} on ${what}` : `Refund on ${what}`;
    case "create_replacement_order":
      return `Replacement order for ${what}`;
    case "send_refund_confirmation":
      return `Refund email to ${what}`;
    case "charge_shipping_fee":
      return amount ? `Shipping fee of ${amount} for ${what}` : `Shipping fee for ${what}`;
    default:
      return `${actionLabel(op.action_type)} · ${what}`;
  }
}

function lower(label: string) {
  return label.charAt(0).toLowerCase() + label.slice(1);
}

/** The effect in words, for "Confirm …" / "Check again whether …". */
function effectPhrase(actionType: string, resourceId: string) {
  const what = lower(resourceLabel(resourceId));
  const number = /#(\d+)$/.exec(resourceLabel(resourceId))?.[1];
  switch (actionType) {
    case "refund_payment": return { done: `the refund on ${what} went through`, thing: `the refund on ${what}` };
    case "create_replacement_order":
      return number
        ? { done: `replacement order #${number} was created`, thing: `replacement order #${number}` }
        : { done: "the replacement order was created", thing: "the replacement order" };
    case "send_refund_confirmation": return { done: "the refund email was delivered", thing: "the refund email" };
    case "charge_shipping_fee": return { done: `the shipping fee for ${what} was charged`, thing: `the shipping fee for ${what}` };
    default: return { done: "this action took effect", thing: "this action" };
  }
}

/** A specific title for a review: "Confirm the refund email was delivered". */
export function proposalTitle(action: string, op?: { action_type: string; resource_id: string }) {
  if (!op) return null;
  const phrase = effectPhrase(op.action_type, op.resource_id);
  if (action === "ATTEST_APPLIED") return `Confirm ${phrase.done}`;
  if (action === "REVERIFY") return `Check again whether ${phrase.done}`;
  if (action === "PROTECTED_RETRY") return `Retry ${phrase.thing}`;
  return null;
}

/**
 * Whether a proposal is still compared with the current record. Only an open
 * or stale proposal is: after approval the record moves on because of the
 * approval itself, which is its result, not a change behind its back.
 */
export function comparesWithCurrent(status: string) {
  return status === "open" || status === "stale";
}

/** What an approved proposal settled, without claiming more than the record shows. */
export function approvedResult(p: { action: string; binding: { seq: number }; current: { state: string } }) {
  const result = p.action === "ATTEST_APPLIED" && p.current.state === "SUCCEEDED"
    ? "Succeeded is the result of this approval."
    : "The current status follows from this approval and any later evidence.";
  return `Approved at the bound version (v${p.binding.seq}). ${result}`;
}

/**
 * "Charge #1001 · Payments (test)" for a grant option; the account and
 * contract version are added only when they are what tells two apart.
 */
export function grantOption(
  grant: { resource_id: string; connector_id: string; provider_account_id: string; contract_version: string },
  all: typeof grant[],
) {
  const base = `${resourceLabel(grant.resource_id)} · ${connectorLabel(grant.connector_id)}`;
  const same = all.filter((g) => g.resource_id === grant.resource_id && g.connector_id === grant.connector_id);
  if (same.length < 2) return base;
  const accounts = new Set(same.map((g) => g.provider_account_id)).size > 1;
  return `${base} (${accounts ? `${grant.provider_account_id}, ` : ""}${grant.contract_version})`;
}

const EFFECT_NOUN: Record<string, string> = {
  refund_payment: "the refund", refund_stripe_test_payment: "the refund",
  create_replacement_order: "the order", send_refund_confirmation: "the email",
  charge_shipping_fee: "the charge",
};
const SEND_RESULT: Record<string, string> = {
  may_have_dispatched: "with no answer", confirmed_applied: "applied",
  proven_not_applied: "not applied", never_dispatched: "not sent",
};

const count = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function breakdown(values: string[], label: (value: string) => string) {
  const tally = new Map<string, number>();
  for (const value of values) tally.set(value, (tally.get(value) ?? 0) + 1);
  return [...tally].map(([value, n]) => `${n} ${label(value)}`).join(", ");
}

/**
 * The evidence in a sentence: what the send itself answered, what lookups
 * found, and any reviewer attestation, instead of a raw record count. A record
 * observed while the action was being sent is the send's answer; any other
 * system record is a lookup. It says nothing about who triggered a lookup.
 */
export function evidenceSummary(op: {
  action_type: string;
  transitions: { seq: number; to_state: string }[];
  attempts: { outcomes: { outcome: string; evidence_kind: string | null; observed_seq: number;
    provenance: string; created_at: string; details?: Record<string, unknown> }[] }[];
}) {
  const stateAt = new Map(op.transitions.map((t) => [t.seq, t.to_state]));
  const escalatedAt = op.transitions.find((t) => t.to_state === "ESCALATED_UNRESOLVED")?.seq;
  const rows = op.attempts.flatMap((a) => a.outcomes);
  const attested = rows.filter((r) => r.provenance === "human_attested");
  const system = rows.filter((r) => r.provenance !== "human_attested");
  const sends = system.filter((r) => stateAt.get(r.observed_seq) === "DISPATCHING");
  const later = system.filter((r) => stateAt.get(r.observed_seq) !== "DISPATCHING");
  // A connector without a lookup records its checks as no_lookup: they are not provider lookups.
  const unchecked = later.filter((r) => r.details?.note === "no_lookup");
  const lookups = later.filter((r) => r.details?.note !== "no_lookup");
  const noun = EFFECT_NOUN[op.action_type] ?? "the effect";

  const parts: string[] = [];
  if (sends.length) {
    const results = [...new Set(sends.map((r) => r.outcome))];
    parts.push(results.length === 1
      ? `${count(sends.length, "send")} ${SEND_RESULT[results[0]!] ?? humanize(results[0]!).toLowerCase()}`
      : `${count(sends.length, "send")}: ${breakdown(sends.map((r) => r.outcome), (o) => SEND_RESULT[o] ?? humanize(o).toLowerCase())}`);
  }
  if (lookups.length) {
    const kinds = [...new Set(lookups.map((r) => r.evidence_kind ?? "inconclusive"))];
    const one: Record<string, string> = {
      not_found: lookups.length === 1 ? `it did not find ${noun}` : `none found ${noun}`,
      inconclusive: lookups.length === 1 ? "it was inconclusive" : "none was conclusive",
      applied: `found ${noun} applied`, proven_not_applied: `proved ${noun} was not applied`,
    };
    const label: Record<string, string> = {
      not_found: "found nothing", inconclusive: "inconclusive", applied: "found it applied",
      proven_not_applied: "proved it was not applied",
    };
    parts.push(kinds.length === 1
      ? `${count(lookups.length, "lookup")}, ${one[kinds[0]!] ?? humanize(kinds[0]!).toLowerCase()}`
      : `${count(lookups.length, "lookup")}: ${breakdown(lookups.map((r) => r.evidence_kind ?? "inconclusive"), (k) => label[k] ?? humanize(k).toLowerCase())}`);
  }
  if (unchecked.length) parts.push(`${count(unchecked.length, "check")} with no provider lookup available`);
  if (attested.length) parts.push(count(attested.length, "reviewer attestation"));

  const after = escalatedAt === undefined ? 0 : lookups.filter((r) => r.observed_seq >= escalatedAt).length;
  const split = after > 0 ? `${lookups.length - after} while investigating, ${after} after escalation` : null;
  const latest = lookups.map((r) => r.created_at).sort().at(-1) ?? null;
  return { text: parts.join(" · "), split, latest, total: rows.length };
}
