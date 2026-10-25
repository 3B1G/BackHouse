// Small chart helpers: scales, ticks, sizing and the pointer/keyboard readout. No chart library.
import { useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";

export const DAY = 86_400_000;
const SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2027-06-25" (or "2027-06", the 1st) as a UTC timestamp. */
export const time = (iso: string) => Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, iso.length > 7 ? +iso.slice(8, 10) : 1);

/** Maps [d0, d1] onto [r0, r1]. */
export function linear(d0: number, d1: number, r0: number, r1: number) {
  const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0);
  return (v: number) => r0 + (v - d0) * k;
}

/** About `count` round values inside [lo, hi]: steps of 1, 2, 2.5 or 5 times a power of ten. */
export function niceTicks(lo: number, hi: number, count: number) {
  if (!(hi > lo)) return [lo];
  const raw = (hi - lo) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10]
    .map((s) => s * mag)
    .reduce((a, b) => (Math.abs(Math.log(b / raw)) < Math.abs(Math.log(a / raw)) ? b : a));
  const ticks: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) ticks.push(Math.round(v));
  return ticks;
}

/** Month starts between two timestamps, every `every` months (January, April, July, October when every is 3). */
export function monthTicks(from: number, to: number, every: number) {
  const ticks: { t: number; label: string }[] = [];
  const start = new Date(from);
  for (let i = 0; ; i++) {
    const t = Date.UTC(start.getUTCFullYear(), start.getUTCMonth() + i, 1);
    if (t > to) break;
    const d = new Date(t);
    const m = d.getUTCMonth();
    if (t >= from && m % every === 0) ticks.push({ t, label: m === 0 ? `Jan ${d.getUTCFullYear()}` : SHORT[m] });
  }
  return ticks;
}

/** How much bigger than the 16px base the page is drawn (the root font grows on big screens), so chart text keeps up. */
export const pageScale = () => (parseFloat(getComputedStyle(document.documentElement).fontSize) || 16) / 16;

/** A centered axis label that hangs inward near an edge instead of being cut off. */
export function inside(x: number, width: number, half = 28): { x: number; anchor: "start" | "middle" | "end" } {
  if (x + half > width) return { x: width - 2, anchor: "end" };
  if (x - half < 0) return { x: 2, anchor: "start" };
  return { x, anchor: "middle" };
}

/** An SVG path through points. */
export const path = (pts: [number, number][]) => pts.map(([a, b], i) => `${i ? "L" : "M"}${a.toFixed(1)} ${b.toFixed(1)}`).join("");

/** Index of the value nearest to v. */
export function nearest(xs: number[], v: number) {
  let best = 0;
  for (let i = 1; i < xs.length; i++) if (Math.abs(xs[i] - v) < Math.abs(xs[best] - v)) best = i;
  return best;
}

/** The element's width, kept up to date. */
export function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWidth(el.clientWidth);
    const observer = new ResizeObserver(([entry]) => setWidth(Math.round(entry.contentRect.width)));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

/** Which point a chart reads out: the one under the pointer, or the one the arrow keys reached. */
export function useScrub(count: number, start: number) {
  const [active, setActive] = useState<number | null>(null);
  const [keyed, setKeyed] = useState(false);
  const at = active === null ? null : Math.min(active, count - 1);
  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === "Escape") return setActive(null);
    const from = at ?? start;
    const to =
      e.key === "ArrowLeft" ? from - 1 : e.key === "ArrowRight" ? from + 1 : e.key === "Home" ? 0 : e.key === "End" ? count - 1 : null;
    if (to === null) return;
    e.preventDefault();
    setKeyed(true);
    setActive(Math.max(0, Math.min(count - 1, to)));
  };
  return {
    at,
    keyed,
    hover: (i: number | null) => {
      setKeyed(false);
      setActive(i);
    },
    focusProps: {
      tabIndex: 0,
      onKeyDown,
      onFocus: () => {
        setKeyed(true);
        setActive((a) => a ?? start);
      },
      onBlur: () => setActive(null),
    },
  };
}
