import { useEffect } from "react";

/** "Actions · SafeAct" in the browser tab. */
export function usePageTitle(title: string | null | undefined) {
  useEffect(() => {
    document.title = title ? `${title} · SafeAct` : "SafeAct";
  }, [title]);
}
