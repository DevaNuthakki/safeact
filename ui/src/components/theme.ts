import { useState } from "react";

// Light is the default whatever the OS prefers; a person may choose
// dark in the header. Remembered per browser only: storage can be
// missing or refuse, and the console then simply stays light.

export type Theme = "light" | "dark";

const KEY = "safeact-theme";

export function storedTheme(): Theme {
  try {
    return localStorage.getItem(KEY) === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

export function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
}

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(storedTheme);
  const choose = (next: Theme) => {
    applyTheme(next);
    setTheme(next);
    try {
      localStorage.setItem(KEY, next);
    } catch { /* Not remembered; this page still switches. */ }
  };
  return [theme, choose] as const;
}
