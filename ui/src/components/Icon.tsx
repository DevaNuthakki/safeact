// A few inline stroke icons, so the console needs no icon dependency.
// Some paths follow Lucide (ISC) and Feather (MIT): see THIRD_PARTY_NOTICES.md.
// Always decorative: the text beside an icon carries the meaning.

const PATHS = {
  robot: "M12 3v3m-2-3h4M5 7h14v13H5V7Zm-3 4v5m20-5v5M9 11h.01M15 11h.01M9 16h6",
  check: "M20 6 9 17l-5-5",
  x: "M18 6 6 18M6 6l12 12",
  clock: "M12 7v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z",
  search: "m21 21-4.3-4.3M11 18a7 7 0 1 1 0-14 7 7 0 0 1 0 14Z",
  alert: "M12 9v4m0 4h.01M10.3 3.9 2.4 17.5A2 2 0 0 0 4.1 20.5h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z",
  minus: "M5 12h14",
  dot: "M12 12h.01",
  arrowLeft: "M19 12H5m7 7-7-7 7-7",
  sun: "M12 3v2m0 14v2M5.6 5.6 7 7m10 10 1.4 1.4M3 12h2m14 0h2M5.6 18.4 7 17M17 7l1.4-1.4M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z",
  moon: "M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8Z",
  inbox: "M22 12h-6l-2 3h-4l-2-3H2m3.5-6.9L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.1Z",
  user: "M20 21a8 8 0 0 0-16 0M12 13a5 5 0 1 0 0-10 5 5 0 0 0 0 10Z",
  shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Zm-3.5-10 2.5 2.5 4.5-5",
  send: "M22 2 11 13M22 2l-7 20-4-9-9-4 20-7Z",
  file: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Zm0 0v6h6M9 13h6m-6 4h4",
  chevronDown: "m6 9 6 6 6-6",
  info: "M12 16v-4m0-4h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z",
  lock: "M7 11V7a5 5 0 0 1 10 0v4M5 11h14v10H5V11Z",
  box: "M21 8 12 3 3 8m18 0v8l-9 5m9-13-9 5m0 8-9-5V8m9 13v-8M3 8l9 5",
  mail: "M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Zm17 1-9 7-9-7",
  card: "M3 6h18v12H3V6Zm0 4h18M7 15h4",
  refund: "M9 14 4 9l5-5M4 9h11a5 5 0 0 1 0 10h-3",
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, className = "icon" }: { name: IconName; className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
         strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      <path d={PATHS[name]} />
    </svg>
  );
}
