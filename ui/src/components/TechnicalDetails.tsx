import type { ReactNode } from "react";

// Raw facts for engineers, folded away by default: identifiers, codes,
// versions and payloads. Everything stays reachable from the keyboard.

export function TechnicalDetails({ children, label = "Technical details", open }: {
  children: ReactNode; label?: string; open?: boolean;
}) {
  return (
    <details className="tech" open={open}>
      <summary>{label}</summary>
      <div className="tech__body">{children}</div>
    </details>
  );
}
