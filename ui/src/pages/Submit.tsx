import { useMutation, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router";

import { ApiError, api } from "../api/client";
import { grantKey } from "../api/grants";
import { currentRequestKey, pendingAction, rememberAction } from "../api/requestKey";
import type { PendingAction } from "../api/requestKey";
import type { Admitted, Grant } from "../api/types";
import { Field, fieldProps } from "../components/Field";
import { actionLabel, dollarsToCents, grantOption, money, resourceLabel } from "../components/labels";
import { Icon } from "../components/Icon";
import { usePageTitle } from "../components/pageTitle";
import { Empty, ErrorState, Loading } from "../components/States";
import { TechnicalDetails } from "../components/TechnicalDetails";
import { useSession } from "../session/SessionProvider";

const ACTIONS: Record<string, { label: string; resourceField: string; resourceLabel: string }> = {
  refund_payment: { label: "Refund a charge", resourceField: "charge_id", resourceLabel: "Charge" },
  create_replacement_order: { label: "Create a replacement order", resourceField: "case_id", resourceLabel: "Support case" },
  send_refund_confirmation: { label: "Send a refund confirmation email", resourceField: "customer_id", resourceLabel: "Customer" },
  // Offered only when the optional Stripe test-mode demo profile granted it.
  refund_stripe_test_payment: { label: "Refund a Stripe test payment", resourceField: "payment_intent", resourceLabel: "Stripe test payment" },
};

const REFUNDS = new Set(["refund_payment", "refund_stripe_test_payment"]);

/** The form a grant opens: a Stripe test-mode refund has its own form and payload. */
function formAction(grant: Grant) {
  return grant.connector_id === "stripe-test" && grant.action_type === "refund_payment"
    ? "refund_stripe_test_payment"
    : grant.action_type;
}

const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const TEMPLATE = /^[a-z][a-z0-9_]{0,63}$/;

export function Submit() {
  usePageTitle("Submit an action");
  const { principal, loading } = useSession();
  if (loading) return <div className="page page--form"><Loading label="Checking your session…" /></div>;
  if (!principal) return <div className="page page--form"><Empty>Sign in to request an action.</Empty></div>;
  const owner = `${principal.tenant_id}.${principal.principal_id}`;
  return <SubmitForm key={owner} owner={owner} />;
}

function SubmitForm({ owner }: { owner: string }) {
  const navigate = useNavigate();
  const [retained] = useState(() => {
    try { return { body: pendingAction(owner), unreadable: false }; }
    catch { return { body: null, unreadable: true }; }
  });
  const [pending, setPending] = useState(retained.body);
  const [storageError, setStorageError] = useState(retained.unreadable);
  const grants = useQuery({
    queryKey: ["grants"],
    queryFn: ({ signal }) => api<Grant[]>("/grants", { signal }),
  });
  const [requestKey] = useState(() => pending?.request_key ?? currentRequestKey(owner));
  const [action, setAction] = useState("");
  const [selectedGrant, setSelectedGrant] = useState("");
  const [amount, setAmount] = useState("");
  const [sku, setSku] = useState("");
  const [template, setTemplate] = useState("refund_done");
  const [touched, setTouched] = useState(false);
  const [blurred, setBlurred] = useState<Record<string, boolean>>({});
  const blur = (name: string) => () => setBlurred((b) => ({ ...b, [name]: true }));

  const available = useMemo(
    () => (grants.data ?? []).filter((g) => formAction(g) in ACTIONS),
    [grants.data],
  );
  const actionTypes = [...new Set(available.map(formAction))];
  const choices = available.filter((g) => formAction(g) === action);
  const grant = choices.find((g) => grantKey(g) === selectedGrant);
  const spec = ACTIONS[action];

  const errors: Record<string, string> = {};
  if (!action) errors.action = "Choose an action.";
  if (action && !grant) errors.resource = "Choose a resource you are allowed to act on.";
  const cents = dollarsToCents(amount);
  if (REFUNDS.has(action) && cents === null) {
    errors.amount = "Enter an amount in dollars between 0.01 and 100,000.00, with at most two decimals.";
  }
  if (action === "create_replacement_order" && !IDENTIFIER.test(sku)) {
    errors.sku = "Enter a SKU: letters, digits, dots, colons, dashes or underscores.";
  }
  if (action === "send_refund_confirmation" && !TEMPLATE.test(template)) {
    errors.template = "Enter a template name: lowercase letters, digits and underscores.";
  }

  const submit = useMutation({
    mutationFn: (body: PendingAction) => api<Admitted>("/actions", { method: "POST", body }),
    onSuccess: (admitted) => {
      // The key is forgotten by the detail page once it is shown, not
      // here: a refresh before then must resubmit as the same operation.
      navigate(`/actions/${admitted.operation_id}`, {
        state: {
          submittedKey: requestKey,
          notice: admitted.created
            ? "Recorded. Any retry must satisfy the connector's safety contract."
            : "This request was already recorded; showing it.",
        },
      });
    },
  });

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    setTouched(true);
    if (submit.isPending || (!pending && Object.keys(errors).length > 0)) return;
    const body: PendingAction = pending ?? {
      request_key: requestKey, action_type: action, provider_account_id: grant!.provider_account_id,
      contract_version: grant!.contract_version,
      payload: {
        [spec!.resourceField]: grant!.resource_id,
        ...(REFUNDS.has(action) ? { amount_minor: cents! } : {}),
        ...(action === "create_replacement_order" ? { sku } : {}),
        ...(action === "send_refund_confirmation" ? { template } : {}),
      },
    };
    try { rememberAction(owner, body); }
    catch { setStorageError(true); return; }
    setStorageError(false);
    setPending(body);
    submit.mutate(body);
  };

  if (grants.isPending) return <div className="page page--form"><Loading label="Loading what you may request…" /></div>;
  if (grants.isError) {
    return <div className="page page--form"><ErrorState error={grants.error} retry={() => grants.refetch()} /></div>;
  }
  if (!pending && actionTypes.length === 0) {
    return (
      <div className="page page--form">
        <Empty>You have no actions you may request.<span className="empty__hint muted"> Ask an administrator for access.</span></Empty>
      </div>
    );
  }

  const show = (name: string) => (touched || blurred[name]) && errors[name];
  const amountPreview = REFUNDS.has(action) && cents !== null ? `Refunds ${money(cents)}` : null;
  const conflict = submit.error instanceof ApiError && submit.error.code === "request_conflict";

  return (
    <div className="page page--form">
      <Link to="/actions" className="back-link"><Icon name="arrowLeft" />All actions</Link>
      <div className="page-header">
        <div>
          <h1>Submit an action</h1>
          <p className="page-header__sub">
            SafeAct records your request before sending it. Track its outcome and evidence on the next screen.
          </p>
        </div>
      </div>
      <form className="panel" onSubmit={onSubmit} noValidate>
        {pending ? <section className="notice info" aria-labelledby="retained-title">
          <h2 id="retained-title">Request retained in this browser</h2>
          <p>The submission outcome is not confirmed here. Retry the same request to recover its result.</p>
          <dl className="facts retained-facts">
            <dt>Action</dt><dd>{actionLabel(pending.action_type)}</dd>
            <dt>Resource</dt><dd>{resourceLabel(String(pending.payload.charge_id ?? pending.payload.payment_intent ?? pending.payload.case_id ?? pending.payload.customer_id ?? ""))}</dd>
            {typeof pending.payload.amount_minor === "number" && <><dt>Amount</dt><dd>{money(pending.payload.amount_minor)}</dd></>}
          </dl>
        </section> : <>
        <Field id="submit-action" label="Action" error={show("action")}>
          <select {...fieldProps("submit-action", undefined, show("action"))} value={action}
                  onBlur={blur("action")}
                  onChange={(e) => { setAction(e.target.value); setSelectedGrant(""); }}>
            <option value="">Choose an action…</option>
            {actionTypes.map((a) => <option key={a} value={a}>{ACTIONS[a]!.label}</option>)}
          </select>
        </Field>

        {spec && (
          <Field id="submit-resource" label={spec.resourceLabel} error={show("resource")}
                 hint="Only resources you are allowed to act on are listed.">
            <select {...fieldProps("submit-resource", true, show("resource"))} value={selectedGrant}
                    onBlur={blur("resource")} onChange={(e) => setSelectedGrant(e.target.value)}>
              <option value="">Choose…</option>
              {choices.map((g) => (
                <option key={grantKey(g)} value={grantKey(g)}>
                  {grantOption(g, choices)}
                </option>
              ))}
            </select>
          </Field>
        )}

        {REFUNDS.has(action) && (
          <Field id="submit-amount" label="Amount (USD)" hint="In dollars, for example 15.00."
                 error={show("amount")} preview={amountPreview}>
            <input {...fieldProps("submit-amount", true, show("amount"))} inputMode="decimal" value={amount}
                   placeholder="15.00" onBlur={blur("amount")} onChange={(e) => setAmount(e.target.value)} />
          </Field>
        )}
        {action === "create_replacement_order" && (
          <Field id="submit-sku" label="SKU" hint="Letters, digits, dots, colons, dashes or underscores."
                 error={show("sku")}>
            <input {...fieldProps("submit-sku", true, show("sku"))} value={sku} placeholder="SKU-REPL-001"
                   onBlur={blur("sku")} onChange={(e) => setSku(e.target.value)} />
          </Field>
        )}
        {action === "send_refund_confirmation" && (
          <Field id="submit-template" label="Email template" hint="Lowercase letters, digits and underscores."
                 error={show("template")}>
            <input {...fieldProps("submit-template", true, show("template"))} value={template}
                   placeholder="refund_done" onBlur={blur("template")} onChange={(e) => setTemplate(e.target.value)} />
          </Field>
        )}

        </>}

        {storageError && <p className="notice bad" role="alert">The request could not be safely retained in browser session storage. No request was sent by this attempt. Enable storage and try again; if a previous submission is uncertain, find it in Actions before starting another.</p>}
        {conflict && (
          <p className="notice bad" role="alert">
            This request key belongs to a different request. Find the original action before starting another.
          </p>
        )}
        {submit.isError && !conflict && <ErrorState error={submit.error} />}
        <div className="form-actions">
          <button type="submit" disabled={submit.isPending || conflict || retained.unreadable}>
            {submit.isPending ? "Submitting…" : pending ? "Retry the same request" : "Submit"}
          </button>
          <Link to="/actions">{pending ? "Find existing action" : "Cancel"}</Link>
        </div>
        <div className="form-footer">
          <TechnicalDetails>
            <p className="hint">
              Request key <code>{requestKey}</code>. Sending the same request again with it returns the
              original action instead of creating a second one. Amounts are sent as whole cents.
            </p>
          </TechnicalDetails>
        </div>
      </form>
      <p className="context-note"><Icon name="lock" />Only actions and resources granted to your account are available. Protected retries remain subject to the provider contract.</p>
    </div>
  );
}
