import { useRef, useState, type KeyboardEvent } from "react";
import { prefer, preferred, type Forecast } from "../api";
import { CashChart } from "./charts/CashChart";
import { MonthChart } from "./charts/MonthChart";
import { SpendChart } from "./charts/SpendChart";

const VIEWS = [
  { id: "cash", label: "Cash" },
  { id: "monthly", label: "Monthly" },
  { id: "spending", label: "Spending" },
] as const;
type View = (typeof VIEWS)[number]["id"];

/** Three ways to see the same money, switched with tabs. The owner's last choice is remembered on this device. */
export function TrendPanel({ f }: { f: Forecast }) {
  const [view, setView] = useState<View>(() => {
    const saved = preferred("boh-chart");
    return VIEWS.find((v) => v.id === saved)?.id ?? "cash";
  });
  const [table, setTable] = useState(false);
  const tabs = useRef<(HTMLButtonElement | null)[]>([]);

  const choose = (next: View, focus = false) => {
    setView(next);
    prefer("boh-chart", next);
    if (focus) tabs.current[VIEWS.findIndex((v) => v.id === next)]?.focus();
  };
  const onKey = (e: KeyboardEvent, i: number) => {
    const n = VIEWS.length;
    const to = e.key === "ArrowRight" ? (i + 1) % n : e.key === "ArrowLeft" ? (i - 1 + n) % n : e.key === "Home" ? 0 : e.key === "End" ? n - 1 : -1;
    if (to < 0) return;
    e.preventDefault();
    choose(VIEWS[to].id, true);
  };

  return (
    <section aria-label="Charts" className="min-w-0">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div role="tablist" aria-label="Choose a chart" className="flex gap-1 rounded-lg bg-check p-1">
          {VIEWS.map((v, i) => {
            const on = v.id === view;
            return (
              <button
                key={v.id}
                ref={(el) => {
                  tabs.current[i] = el;
                }}
                type="button"
                role="tab"
                id={`tab-${v.id}`}
                aria-selected={on}
                aria-controls="trend-panel"
                tabIndex={on ? 0 : -1}
                onClick={() => choose(v.id)}
                onKeyDown={(e) => onKey(e, i)}
                className={`inline-flex min-h-11 items-center rounded-md px-4 text-[0.9375rem] font-bold transition-colors duration-150 ${
                  on ? "bg-paper text-ink shadow-[0_0_0_1px_var(--color-rule)]" : "text-pencil hover:bg-check-deep hover:text-ink"
                }`}
              >
                {v.label}
              </button>
            );
          })}
        </div>
        <button
          type="button"
          onClick={() => setTable((t) => !t)}
          className="min-h-11 rounded-md px-3 text-sm font-bold text-ink underline underline-offset-4 hover:bg-check"
        >
          {table ? "Show as chart" : "Show as table"}
        </button>
      </div>
      <div
        role="tabpanel"
        id="trend-panel"
        aria-labelledby={`tab-${view}`}
        className="mt-5"
      >
        {view === "cash" ? (
          <CashChart f={f} table={table} />
        ) : view === "monthly" ? (
          <MonthChart f={f} table={table} />
        ) : (
          <SpendChart f={f} table={table} />
        )}
      </div>
    </section>
  );
}
