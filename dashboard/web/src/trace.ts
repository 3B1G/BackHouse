// The developer view's live feed of agent calls (/api/dev/trace), and small formatters for it.
import { useEffect, useState } from "react";

export type TraceKind = "start" | "done" | "waiting" | "error" | "call" | "log" | "result" | "check";
export type TraceEvent = {
  id: number;
  at: number;
  agent: string;
  kind: TraceKind;
  text: string;
  group: string | null;
  ms: number | null;
  detail: Record<string, unknown> | null;
};

/** Every agent call from the API as it happens. A burst of events renders once per frame. */
export function useTrace() {
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [live, setLive] = useState(false);
  useEffect(() => {
    const seen = new Set<string>(); // a reconnect may replay what the API still has in memory
    let queue: TraceEvent[] = [];
    let frame = 0;
    const source = new EventSource("/api/dev/trace");
    source.onopen = () => setLive(true);
    source.onerror = () => setLive(false);
    source.onmessage = (m) => {
      const e = JSON.parse(m.data) as TraceEvent;
      const key = `${e.id}-${e.at}`;
      if (seen.has(key)) return;
      seen.add(key);
      queue.push(e);
      if (!frame)
        frame = requestAnimationFrame(() => {
          const batch = queue;
          queue = [];
          frame = 0;
          setEvents((list) => [...list, ...batch].slice(-1500));
        });
    };
    return () => {
      source.close();
      cancelAnimationFrame(frame);
    };
  }, []);
  return { events, live };
}

/** Seconds since the epoch, ticking. */
export function useNow(ms: number) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now() / 1000), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}

export const traceKey = (e: TraceEvent) => `${e.id}-${e.at}`;
export const seconds = (ms: number) => (ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);
export const clockTime = (at: number) => {
  const d = new Date(at * 1000);
  const two = (n: number) => String(n).padStart(2, "0");
  return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}.${String(d.getMilliseconds()).padStart(3, "0")}`;
};
