import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router";

import { ApiError, api, newRequestKey } from "../api/client";
import { grantKey } from "../api/grants";
import type { CreateWorkflow, Grant, WorkflowAdmitted } from "../api/types";
import { pendingWorkflow, rememberWorkflow } from "../api/workflowRequest";
import { Field, fieldProps } from "../components/Field";
import { Icon } from "../components/Icon";
import { centsToDollars, dollarsToCents, grantOption, money, resourceLabel } from "../components/labels";
import { usePageTitle } from "../components/pageTitle";
import { Empty, ErrorState, Loading } from "../components/States";
import { TechnicalDetails } from "../components/TechnicalDetails";
import { useSession } from "../session/SessionProvider";

export function WorkflowSubmit() {
  usePageTitle("Send a replacement");
  const { principal, loading } = useSession();
  if (loading) return <div className="page page--form"><Loading label="Checking your session…" /></div>;
  if (!principal) return <div className="page page--form"><Empty>Sign in to request a replacement.</Empty></div>;
  const owner = `${principal.tenant_id}.${principal.principal_id}`;
  return <WorkflowForm key={owner} owner={owner} />;
}

function WorkflowForm({ owner }: { owner: string }) {
  const navigate = useNavigate();
  const [pending, setPending] = useState(() => pendingWorkflow(owner));
  const [requestKey] = useState(() => pending?.request_key ?? newRequestKey());
  const [customer, setCustomer] = useState("");
  const [supportCase, setSupportCase] = useState("");
  const [sku, setSku] = useState(pending?.sku ?? "");
  const [fee, setFee] = useState(pending ? centsToDollars(pending.fee_minor) : "");
  const [touched, setTouched] = useState(false);
  const [blurred, setBlurred] = useState<Record<string, boolean>>({});
  const blur = (name: string) => () => setBlurred((b) => ({ ...b, [name]: true }));
  const [storageError, setStorageError] = useState(false);
  const grants = useQuery({
    queryKey: ["grants"],
    queryFn: ({ signal }) => api<Grant[]>("/grants", { signal }),
  });
  const charges = (grants.data ?? []).filter((g) =>
    g.action_type === "charge_shipping_fee" && g.connector_id === "mock-payments");
  const orders = (grants.data ?? []).filter((g) =>
    g.action_type === "create_replacement_order" && g.connector_id === "mock-crm");
  const charge = charges.find((g) => grantKey(g) === customer);
  const order = orders.find((g) => grantKey(g) === supportCase);
  const errors: Record<string, string> = {};
  if (!charge) errors.customer = "Choose a customer to charge.";
  if (!order) errors.case = "Choose a support case.";
  if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(sku)) {
    errors.sku = "Enter a SKU: letters, digits, dots, colons, dashes or underscores.";
  }
  const feeCents = dollarsToCents(fee);
  if (feeCents === null) {
    errors.fee = "Enter a fee in dollars between 0.01 and 100,000.00, with at most two decimals.";
  }
  const valid = Object.keys(errors).length === 0;
  const show = (name: string) => (touched || blurred[name]) && errors[name];

  const submit = useMutation({
    mutationFn: (body: CreateWorkflow) => api<WorkflowAdmitted>("/workflows", { method: "POST", body }),
    onSuccess: ({ workflow_id }) => navigate(`/workflows/${workflow_id}`),
  });
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    setTouched(true);
    if (submit.isPending || (!pending && !valid)) return;
    const body: CreateWorkflow = pending ?? {
      request_key: requestKey,
      customer_id: charge!.resource_id, case_id: order!.resource_id, sku,
      fee_minor: feeCents!, payments_account_id: charge!.provider_account_id,
      crm_account_id: order!.provider_account_id, charge_contract: charge!.contract_version,
      order_contract: order!.contract_version, compensation_contract: charge!.contract_version,
    };
    try {
      rememberWorkflow(owner, body);
    } catch {
      // Refuse before dispatch if we cannot retain the identity across reloads.
      setStorageError(true);
      return;
    }
    setStorageError(false);
    setPending(body);
    submit.mutate(body);
  };

  if (grants.isPending) return <div className="page page--form"><Loading label="Loading workflow permissions…" /></div>;
  if (grants.isError) {
    return <div className="page page--form"><ErrorState error={grants.error} retry={() => grants.refetch()} /></div>;
  }
  if (!pending && (!charges.length || !orders.length)) {
    return (
      <div className="page page--form">
        <Empty>You need permission to charge a customer and create a replacement order.</Empty>
      </div>
    );
  }
  const conflict = submit.error instanceof ApiError && submit.error.code === "request_conflict";
  return (
    <div className="page page--workflow-form">
      <Link to="/actions" className="back-link"><Icon name="arrowLeft" />All actions</Link>
      <div className="page-header">
        <div>
          <h1>Send a replacement</h1>
          <p className="page-header__sub">Charge a shipping fee, then create the replacement order. If the
            order is confirmed not created, SafeAct requests a separate refund of that charge.</p>
        </div>
      </div>
      <div className="workflow-form-layout">
      <div className="stack">
        <p className="notice warn">An uncertain order may already exist. SafeAct investigates it
          before deciding whether a refund is allowed.</p>
        <form className="panel" onSubmit={onSubmit} noValidate>
          {pending ? (
            <div className="notice info">
              <p className="notice__title">Submission retained.</p>
              <p>Retry checks the same request key.</p>
              <dl className="facts">
                <dt>Customer</dt><dd>{resourceLabel(pending.customer_id)}</dd>
                <dt>Support case</dt><dd>{resourceLabel(pending.case_id)}</dd>
                <dt>Replacement item (SKU)</dt><dd>{pending.sku}</dd>
                <dt>Shipping fee</dt><dd>{money(pending.fee_minor)}</dd>
              </dl>
            </div>
          ) : <>
            <Field id="workflow-customer" label="Customer to charge" error={show("customer")}>
              <select {...fieldProps("workflow-customer", undefined, show("customer"))} value={customer}
                      onBlur={blur("customer")} onChange={(e) => setCustomer(e.target.value)}>
                <option value="">Choose a customer</option>
                {charges.map((g) => <option key={grantKey(g)} value={grantKey(g)}>
                  {grantOption(g, charges)}</option>)}
              </select>
            </Field>
            <Field id="workflow-case" label="Support case" error={show("case")}>
              <select {...fieldProps("workflow-case", undefined, show("case"))} value={supportCase}
                      onBlur={blur("case")} onChange={(e) => setSupportCase(e.target.value)}>
                <option value="">Choose a support case</option>
                {orders.map((g) => <option key={grantKey(g)} value={grantKey(g)}>
                  {grantOption(g, orders)}</option>)}
              </select>
            </Field>
            <Field id="workflow-sku" label="Replacement SKU" error={show("sku")}
                   hint="Letters, digits, dots, colons, dashes or underscores.">
              <input {...fieldProps("workflow-sku", true, show("sku"))} value={sku} placeholder="SKU-REPL-001"
                     onBlur={blur("sku")} onChange={(e) => setSku(e.target.value)} />
            </Field>
            <Field id="workflow-fee" label="Shipping fee (USD)" error={show("fee")}
                   hint="In dollars, for example 5.00."
                   preview={feeCents !== null ? `Charges ${money(feeCents)}` : null}>
              <input {...fieldProps("workflow-fee", true, show("fee"))} inputMode="decimal" value={fee}
                     placeholder="5.00" onBlur={blur("fee")} onChange={(e) => setFee(e.target.value)} />
            </Field>
          </>}
          {storageError && <p className="notice bad" role="alert">Enable browser session storage before submitting. No request was sent.</p>}
          {submit.isError && <ErrorState error={submit.error} />}
          {conflict && <p className="notice bad" role="alert">This key belongs to a different request. Ask an investigator to
            locate the original workflow before creating another.</p>}
          <div className="form-actions">
            <button type="submit" disabled={submit.isPending || conflict}>
              {submit.isPending ? "Recording…" : pending ? "Retry the same request" : "Start replacement workflow"}
            </button>
            <Link to="/actions">{pending ? "Find existing action" : "Cancel"}</Link>
          </div>
          <div className="form-footer">
            <TechnicalDetails>
              <p className="hint">Request key <code>{requestKey}</code>. The fee is sent as whole cents.</p>
            </TechnicalDetails>
          </div>
        </form>
      </div>
      <aside className="panel workflow-explainer" aria-labelledby="replacement-how">
        <h2 id="replacement-how">How this works</h2>
        <ol className="workflow-explainer__steps">
          <li><strong>Charge the shipping fee</strong><p>The charge must succeed before an order is started.</p></li>
          <li><strong>Create the replacement order</strong><p>Each step has its own recorded outcome and evidence.</p></li>
        </ol>
        <div className="conditional-note"><Icon name="refund" /><div><strong>Refund only after confirmed order failure</strong><p>An uncertain order may already exist. SafeAct investigates instead of assuming a refund is safe.</p></div></div>
      </aside>
      </div>
    </div>
  );
}
