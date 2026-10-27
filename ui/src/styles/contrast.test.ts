import { readFileSync } from "node:fs";
import { join } from "node:path";

// The design tokens' text/background pairs meet WCAG AA in both themes:
// 4.5:1 for text, 3:1 for UI boundaries, icons and the focus ring.

const css = readFileSync(join(__dirname, "app.css"), "utf8");

function tokens(selector: string) {
  const start = css.indexOf(`${selector} {`);
  const block = css.slice(start, css.indexOf("\n}", start));
  return Object.fromEntries([...block.matchAll(/--([a-z0-9-]+):\s*(#[0-9a-f]{6});/g)].map((m) => [m[1]!, m[2]!]));
}

const light = tokens(":root");
const dark = { ...light, ...tokens(':root[data-theme="dark"]') };

function luminance(hex: string) {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
}

function ratio(a: string, b: string) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi! + 0.05) / (lo! + 0.05);
}

const TEXT: [string, string][] = [
  ["text", "bg"], ["text", "surface"], ["text", "surface-2"],
  ["text-muted", "bg"], ["text-muted", "surface"], ["text-muted", "surface-2"],
  ["accent", "bg"], ["accent", "surface"], ["accent", "surface-2"], ["accent-hover", "surface"],
  ["accent-text", "accent"], ["accent-text", "accent-hover"],
  ["bad-text", "surface"],
  ...["ok", "warn", "bad", "info", "neutral"].map((t) => [`${t}-text`, `${t}-bg`] as [string, string]),
];
const UI: [string, string][] = [
  ["focus", "bg"], ["focus", "surface"], ["border-strong", "surface"], ["border-strong", "bg"],
  ["ok-text", "surface"], ["warn-text", "surface"], ["info-text", "surface"],
];

describe.each([["light", light], ["dark", dark]] as const)("%s theme", (_name, theme) => {
  it.each(TEXT)("%s on %s is at least 4.5:1", (fg, bg) => {
    expect(ratio(theme[fg]!, theme[bg]!)).toBeGreaterThanOrEqual(4.5);
  });
  it.each(UI)("%s on %s is at least 3:1", (fg, bg) => {
    expect(ratio(theme[fg]!, theme[bg]!)).toBeGreaterThanOrEqual(3);
  });
});
