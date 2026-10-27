import { useEffect, useId, useRef, useState } from "react";

import type { Principal } from "../api/types";
import { Icon } from "./Icon";
import { companyName, initials, personTitle } from "./people";

// The signed-in person: an initials button that opens a small panel with
// name, role, company and Sign out. A disclosure, not an ARIA menu:
// Escape or a click outside closes it and focus returns to the button.

export function UserMenu({ principal, onSignOut }: { principal: Principal; onSignOut: () => void }) {
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  const root = useRef<HTMLDivElement>(null);
  const panelId = useId();

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        button.current?.focus();
      }
    };
    const onPointer = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointer);
    };
  }, [open]);

  const title = personTitle(principal);
  const company = companyName(principal.tenant_id);
  return (
    <div className="user-menu" ref={root}>
      <button ref={button} type="button" className="user-menu__button" aria-expanded={open} aria-controls={panelId}
              aria-label={`Account: ${principal.display_name}`}
              onClick={() => setOpen(!open)}>
        <span className="avatar" aria-hidden="true">{initials(principal.display_name)}</span>
        <span className="visually-hidden">Account: </span>
        <span className="user-menu__identity"><span>{principal.display_name}</span><span className="user-menu__meta">{title}</span></span>
        <Icon name="chevronDown" className="icon user-menu__chevron" />
      </button>
      <div id={panelId} className="user-menu__panel" hidden={!open}>
        <div className="user-menu__who">
          <span className="avatar avatar--lg" aria-hidden="true">{initials(principal.display_name)}</span>
          <span>
            <span className="user-menu__name">{principal.display_name}</span>
            <span className="user-menu__meta">{title}</span>
            <span className="user-menu__meta">{company}</span>
          </span>
        </div>
        {principal.demo && <p className="user-menu__note">Demo account · synthetic data only</p>}
        <button type="button" className="secondary block" onClick={onSignOut}>Sign out</button>
      </div>
    </div>
  );
}
