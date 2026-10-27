import { useEffect, useState } from "react";

/** The current time, refreshed every `everyMs` while `active`. */
export function useNow(active = true, everyMs = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return undefined;
    const timer = setInterval(() => setNow(Date.now()), everyMs);
    return () => clearInterval(timer);
  }, [active, everyMs]);
  return now;
}

/** Precise distance to a "not before" time, for scheduled checks. */
export function relative(target: Date, now: number) {
  const seconds = Math.round((target.getTime() - now) / 1000);
  if (Math.abs(seconds) < 1) return "now";
  const span = Math.abs(seconds);
  const text = span < 120 ? `${span} s` : span < 7200
    ? `${Math.round(span / 60)} min` : `${Math.round(span / 3600)} h`;
  return seconds > 0 ? `in ${text}` : `${text} ago (at the next pass)`;
}

/** Coarse distance for lists: "just now", "4 min ago", "in 2 h". */
export function ago(target: Date, now: number) {
  const seconds = Math.round((now - target.getTime()) / 1000);
  const span = Math.abs(seconds);
  if (span < 45) return seconds >= 0 ? "just now" : "in a moment";
  const text = span < 5400 ? `${Math.max(1, Math.round(span / 60))} min`
    : span < 129_600 ? `${Math.round(span / 3600)} h` : `${Math.round(span / 86_400)} d`;
  return seconds >= 0 ? `${text} ago` : `in ${text}`;
}

/** A relative time with the exact local time on hover. */
export function RelativeTime({ value }: { value: string }) {
  const now = useNow(true, 30_000);
  const date = new Date(value);
  return (
    <time dateTime={value} title={date.toLocaleString()} className="num">
      {ago(date, now)}
      <span className="visually-hidden"> ({date.toLocaleString()})</span>
    </time>
  );
}
