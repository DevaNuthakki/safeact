import { useRef } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import { useSearchParams } from "react-router";

// Accessible tabs (WAI-ARIA tabs pattern): arrow keys, Home and End move
// between tabs; the chosen tab is kept in ?tab= so it survives a reload
// and can be linked to.

export interface Tab { id: string; label: string; content: ReactNode }

export function Tabs({ tabs, label }: { tabs: Tab[]; label: string }) {
  const [params, setParams] = useSearchParams();
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const requested = params.get("tab");
  const current = tabs.find((t) => t.id === requested) ?? tabs[0]!;

  const select = (id: string, focus = false) => {
    const next = new URLSearchParams(params);
    if (id === tabs[0]!.id) next.delete("tab");
    else next.set("tab", id);
    setParams(next, { replace: true, preventScrollReset: true });
    if (focus) refs.current[tabs.findIndex((t) => t.id === id)]?.focus();
  };

  const onKeyDown = (event: KeyboardEvent, index: number) => {
    const last = tabs.length - 1;
    const target = { ArrowRight: index === last ? 0 : index + 1, ArrowLeft: index === 0 ? last : index - 1,
      Home: 0, End: last }[event.key];
    if (target === undefined) return;
    event.preventDefault();
    select(tabs[target]!.id, true);
  };

  return (
    <div className="tabs">
      <div className="tabs__list" role="tablist" aria-label={label}>
        {tabs.map((tab, index) => {
          const selected = tab.id === current.id;
          return (
            <button key={tab.id} type="button" role="tab" id={`tab-${tab.id}`}
                    ref={(node) => { refs.current[index] = node; }}
                    aria-selected={selected} aria-controls={`panel-${tab.id}`} tabIndex={selected ? 0 : -1}
                    onClick={() => select(tab.id)} onKeyDown={(e) => onKeyDown(e, index)}>
              {tab.label}
            </button>
          );
        })}
      </div>
      <div className="tabs__panel" role="tabpanel" id={`panel-${current.id}`} aria-labelledby={`tab-${current.id}`}
           tabIndex={0}>
        {current.content}
      </div>
    </div>
  );
}
