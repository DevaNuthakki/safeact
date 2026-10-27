import type { ReactNode } from "react";

import { Icon } from "./Icon";

// One labelled form field: hint and error are tied to the control with
// aria-describedby, and the control is marked invalid only when its
// error is shown.

export function fieldProps(id: string, hint?: ReactNode, error?: string | false | null) {
  const describedBy = [hint ? `${id}-hint` : "", error ? `${id}-error` : ""].filter(Boolean).join(" ");
  return { id, "aria-invalid": Boolean(error), "aria-describedby": describedBy || undefined };
}

export function Field({ id, label, hint, error, preview, children }: {
  id: string; label: ReactNode; hint?: ReactNode; error?: string | false | null; preview?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children}
      {hint && <span id={`${id}-hint`} className="hint">{hint}</span>}
      {preview && !error && <p className="field-preview" aria-live="polite">{preview}</p>}
      {error && <p id={`${id}-error`} className="field-error"><Icon name="alert" />{error}</p>}
    </div>
  );
}
